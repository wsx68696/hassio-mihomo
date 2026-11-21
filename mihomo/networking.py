"""Recover only the kernel objects owned by this app's Mihomo instance."""
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

from config import (atomic_json, atomic_text, TUN_DEVICE, ROUTE_TABLE, RULE_PRIORITY,
                    RULE_LAST, FALLBACK_PRIORITY, INPUT_MARK, OUTPUT_MARK)


def dictionaries(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from dictionaries(child)
    elif isinstance(value, list):
        for child in value:
            yield from dictionaries(child)


def number(value):
    try:
        return int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return None


def has_mark(document, expected):
    for item in dictionaries(document):
        for action, expression, value in [("match", "left", "right"), ("mangle", "key", "value")]:
            statement = item.get(action)
            if not isinstance(statement, dict):
                continue
            key = statement.get(expression)
            if not isinstance(key, dict):
                continue
            for source in ("meta", "ct"):
                accessor = key.get(source)
                if isinstance(accessor, dict) and accessor.get("key") == "mark" and number(statement.get(value)) == expected:
                    return True
    return False


def uses_tun(document):
    found = set()
    for item in dictionaries(document):
        statement = item.get("match")
        if not isinstance(statement, dict):
            continue
        left = statement.get("left")
        if isinstance(left, dict) and isinstance(left.get("meta"), dict):
            key = left["meta"].get("key")
            if key in {"iifname", "oifname"} and statement.get("right") == TUN_DEVICE:
                found.add(key)
    return found == {"iifname", "oifname"}


class NetworkOwner:
    def __init__(self, data):
        self.data = Path(data)
        self.path = self.data / "owned-network.json"
        self.ledger = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        identity = self.data / "network-owner-id"
        if not identity.exists():
            previous = self.ledger.get("owner")
            recovered = previous if isinstance(previous, str) and re.fullmatch(r"[0-9a-f]{32}", previous) else uuid.uuid4().hex
            atomic_text(identity, recovered)
        self.owner = identity.read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"[0-9a-f]{32}", self.owner):
            raise RuntimeError("Mihomo network ownership identifier is invalid")
        self.marker = "haos_mihomo_" + self.owner
        self.claimed = self.ledger.get("owner") == self.owner and self.ledger.get("active") is True
        self.legacy = {}
        self.known_config = False
        configured = self.data / "config.json"
        if configured.exists():
            try:
                tun = json.loads(configured.read_text(encoding="utf-8")).get("tun", {})
                self.known_config = (tun.get("device") == TUN_DEVICE and tun.get("iproute2-table-index") == ROUTE_TABLE
                                     and tun.get("auto-redirect-input-mark") == INPUT_MARK
                                     and tun.get("auto-redirect-output-mark") == OUTPUT_MARK)
                if (tun.get("device") == TUN_DEVICE and tun.get("auto-redirect-output-mark") == OUTPUT_MARK
                        and tun.get("iproute2-table-index", 2022) != ROUTE_TABLE):
                    self.legacy = tun
            except (ValueError, AttributeError):
                pass

    def execute(self, *args, missing=False):
        result = subprocess.run(list(args), capture_output=True, text=True, timeout=5)
        if result.returncode:
            text = result.stderr.lower()
            if missing and any(message in text for message in ("no such file", "does not exist", "cannot find")):
                return None
            raise RuntimeError("Mihomo kernel resource operation failed: " + args[0])
        return result.stdout

    def table(self):
        raw = self.execute("nft", "-j", "list", "table", "inet", "mihomo", missing=True)
        return None if raw is None else json.loads(raw)

    def owns(self, document):
        if document is None:
            return False
        chains = [item["chain"].get("name") for item in dictionaries(document) if isinstance(item.get("chain"), dict)]
        # An explicit marker of another installation always takes precedence.
        if any(isinstance(name, str) and name.startswith("haos_mihomo_") and name != self.marker for name in chains):
            return False
        if self.marker in chains:
            return True
        # Current Mihomo can redirect by interface instead of installing
        # mark expressions. Require both directions and our persisted claim.
        if uses_tun(document) and (self.claimed or self.known_config or self.legacy):
            return True
        if (has_mark(document, OUTPUT_MARK) and self.legacy) or (
            has_mark(document, OUTPUT_MARK) and has_mark(document, INPUT_MARK) and (self.claimed or self.known_config)
        ):
            return True
        # The write-ahead ownership record also covers a kill during creation
        # of our still-empty table, before rule and marker installation.
        rules = [item for item in dictionaries(document) if "rule" in item]
        return self.claimed and not chains and not rules

    def cleanup_rules(self, table, first, last, fallback=None):
        for family in ("-4", "-6"):
            raw = self.execute("ip", family, "-j", "rule", "show")
            for rule in json.loads(raw):
                priority = number(rule.get("priority"))
                if priority is None or priority in {0, 32766, 32767}:
                    continue
                if first <= priority <= last or priority == fallback or number(rule.get("table")) == table:
                    self.execute("ip", family, "rule", "del", "priority", str(priority))
            self.execute("ip", family, "route", "flush", "table", str(table), missing=True)

    def cleanup(self):
        table = self.table()
        own_table = self.owns(table)
        if table is not None and not own_table:
            return  # A foreign instance's table and associated routes are untouched.
        if own_table:
            self.execute("nft", "delete", "table", "inet", "mihomo", missing=True)
        if self.claimed or (own_table and self.known_config):
            self.cleanup_rules(ROUTE_TABLE, RULE_PRIORITY, RULE_LAST, FALLBACK_PRIORITY)
        elif own_table and self.legacy:
            self.cleanup_rules(self.legacy.get("iproute2-table-index", 2022),
                               self.legacy.get("iproute2-rule-index", 9000),
                               self.legacy.get("iproute2-rule-index", 9000) + 20)
        self.claimed = False
        atomic_json(self.path, {"owner": self.owner, "active": False})

    def prepare(self):
        self.cleanup()
        if self.table() is not None:
            raise RuntimeError("发现其他 Mihomo 实例的防火墙表，请先停止该实例")
        for family in ("-4", "-6"):
            rules = json.loads(self.execute("ip", family, "-j", "rule", "show"))
            if any(RULE_PRIORITY <= (number(rule.get("priority")) or 0) <= RULE_LAST
                   or number(rule.get("priority")) == FALLBACK_PRIORITY
                   or number(rule.get("table")) == ROUTE_TABLE for rule in rules):
                raise RuntimeError("Mihomo 的专用路由范围已被其他服务使用")
            routes = self.execute("ip", family, "-j", "route", "show", "table", str(ROUTE_TABLE), missing=True)
            if routes and json.loads(routes):
                raise RuntimeError("Mihomo 的专用路由表已被其他服务使用")
        atomic_json(self.path, {"owner": self.owner, "active": True, "table": ROUTE_TABLE,
                              "first": RULE_PRIORITY, "last": RULE_LAST, "fallback": FALLBACK_PRIORITY})
        self.claimed = True

    def stamp(self):
        document = self.table()
        if document is None:
            return
        if not self.owns(document):
            raise RuntimeError("Mihomo 防火墙表的归属验证失败")
        if any(isinstance(item.get("chain"), dict) and item["chain"].get("name") == self.marker
               for item in dictionaries(document)):
            return
        self.execute("nft", "add", "chain", "inet", "mihomo", self.marker)


class InstanceLock:
    def __init__(self, data):
        self.path = Path(data) / "controller.lock"
        self.stream = None

    def __enter__(self):
        import fcntl
        self.stream = self.path.open("a")
        try:
            fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise RuntimeError("已有 Mihomo 管理进程使用此数据目录") from None
        return self

    def __exit__(self, *_):
        self.stream.close()
