import json
from pathlib import Path
from dataclasses import dataclass, field

from core.connector import AccountConfig

CONFIG_FILE = Path(__file__).parent.parent / "config.json"


@dataclass
class LotRange:
    from_lot: float
    to_lot: float
    lot: float

    def matches(self, volume: float) -> bool:
        return self.from_lot <= volume <= self.to_lot


@dataclass
class VolatilityLotBoost:
    enabled: bool
    extra_lot: float
    trigger_lot: float
    symbols: list[str]

    def applies_to(self, symbol: str, master_volume: float) -> bool:
        if not self.enabled:
            return False
        if round(master_volume, 2) != round(self.trigger_lot, 2):
            return False
        normalized = symbol.strip().lower()
        return any(normalized == s.strip().lower() for s in self.symbols)


@dataclass
class SymbolLotRule:
    copy_lots: dict[float, float] = field(default_factory=dict)
    default_multiplier: float | None = None
    max_lot: float | None = None

    def resolve_lot(self, master_volume: float) -> float | None:
        vol = round(master_volume, 2)

        for source_lot, target_lot in self.copy_lots.items():
            if round(source_lot, 2) == vol:
                lot = target_lot
                if self.max_lot is not None:
                    lot = min(lot, self.max_lot)
                return round(lot, 2)

        if self.default_multiplier is not None:
            lot = round(master_volume * self.default_multiplier, 2)
            if self.max_lot is not None:
                lot = min(lot, self.max_lot)
            return lot

        return None


@dataclass
class CopySettings:
    recalculate_lot: bool
    lot_mode: str
    lot_value: float
    lot_ranges: list[LotRange]
    poll_interval: float
    fallback_interval: float
    max_slippage: int
    volatility_lot_boost: VolatilityLotBoost
    symbol_lot_rules: dict[str, SymbolLotRule] = field(default_factory=dict)

    def find_symbol_rule(self, symbol: str) -> SymbolLotRule | None:
        normalized = symbol.strip().lower()
        for key, rule in self.symbol_lot_rules.items():
            if key.strip().lower() == normalized:
                return rule
        return None


@dataclass
class FollowerConfig:
    account: AccountConfig
    copy_settings: CopySettings


@dataclass
class Settings:
    master: AccountConfig
    followers: list[FollowerConfig]
    copy: CopySettings


def _parse_account(data: dict, fallback_label: str) -> AccountConfig:
    required = ["login", "password", "server", "path"]
    missing = [k for k in required if k not in data]
    if missing:
        raise ValueError(f"Faltan campos en la cuenta '{fallback_label}': {missing}")

    return AccountConfig(
        login=int(data["login"]),
        password=data["password"],
        server=data["server"],
        path=data["path"],
        label=data.get("label", fallback_label),
    )


def _parse_lot_ranges(raw: list[dict]) -> list[LotRange]:
    ranges = []
    for i, r in enumerate(raw):
        missing = [k for k in ("from", "to", "lot") if k not in r]
        if missing:
            raise ValueError(f"lot_ranges[{i}] le faltan campos: {missing}")
        ranges.append(LotRange(
            from_lot=float(r["from"]),
            to_lot=float(r["to"]),
            lot=float(r["lot"]),
        ))
    return sorted(ranges, key=lambda r: r.from_lot)


def _parse_volatility_lot_boost(raw: dict | None) -> VolatilityLotBoost:
    raw = raw or {}
    return VolatilityLotBoost(
        enabled=bool(raw.get("enabled", False)),
        extra_lot=float(raw.get("extra_lot", 0.0)),
        trigger_lot=float(raw.get("trigger_lot", 0.2)),
        symbols=list(raw.get("symbols", [])),
    )


def _parse_symbol_lot_rules(raw: dict | None) -> dict[str, SymbolLotRule]:
    raw = raw or {}
    rules: dict[str, SymbolLotRule] = {}

    for symbol, cfg in raw.items():
        if not isinstance(cfg, dict):
            raise ValueError(f"symbols['{symbol}'] debe ser un objeto.")

        raw_copy_lots = cfg.get("copy_lots") or cfg.get("copyLots") or {}
        copy_lots = {float(k): float(v) for k, v in raw_copy_lots.items()}

        multiplier = cfg.get("default_multiplier", cfg.get("defaultMultiplier"))
        max_lot = cfg.get("max_lot", cfg.get("maxLot"))

        rules[symbol] = SymbolLotRule(
            copy_lots=copy_lots,
            default_multiplier=float(multiplier) if multiplier is not None else None,
            max_lot=float(max_lot) if max_lot is not None else None,
        )

    return rules


def _parse_follower_copy_settings(raw_cs: dict, global_cs: CopySettings) -> CopySettings:
    """Construye un CopySettings para un follower usando el global como base y aplicando overrides."""
    lot_mode = raw_cs.get("lot_mode", global_cs.lot_mode).lower()

    raw_ranges = raw_cs.get("lot_ranges")
    lot_ranges = _parse_lot_ranges(raw_ranges) if raw_ranges is not None else global_cs.lot_ranges

    if lot_mode == "range" and not lot_ranges:
        raise ValueError("lot_mode='range' requiere al menos un rango en 'lot_ranges'.")

    raw_symbols = raw_cs.get("symbols")
    symbol_lot_rules = (
        _parse_symbol_lot_rules(raw_symbols)
        if raw_symbols is not None
        else global_cs.symbol_lot_rules
    )

    return CopySettings(
        recalculate_lot=bool(raw_cs.get("recalculate_lot", global_cs.recalculate_lot)),
        lot_mode=lot_mode,
        lot_value=float(raw_cs.get("lot_value", global_cs.lot_value)),
        lot_ranges=lot_ranges,
        poll_interval=global_cs.poll_interval,
        fallback_interval=global_cs.fallback_interval,
        max_slippage=int(raw_cs.get("max_slippage", global_cs.max_slippage)),
        volatility_lot_boost=_parse_volatility_lot_boost(raw_cs.get("volatility_lot_boost"))
        if raw_cs.get("volatility_lot_boost") is not None
        else global_cs.volatility_lot_boost,
        symbol_lot_rules=symbol_lot_rules,
    )


def load_settings(config_path: Path | None = None) -> Settings:
    path = config_path or CONFIG_FILE

    if not path.exists():
        raise FileNotFoundError(
            f"No se encontró '{path.name}'. "
            f"Copia 'config.json.example' a 'config.json' y completa los datos."
        )

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    master = _parse_account(data.get("master", {}), "MASTER")

    raw_followers = data.get("followers", [])
    if not raw_followers:
        raise ValueError("Se requiere al menos un follower en 'followers'.")

    cs = data.get("copy_settings", {})
    lot_mode = cs.get("lot_mode", "fixed").lower()
    lot_ranges = _parse_lot_ranges(cs.get("lot_ranges", []))

    if lot_mode == "range" and not lot_ranges:
        raise ValueError("lot_mode='range' requiere definir al menos un rango en 'lot_ranges'.")

    copy = CopySettings(
        recalculate_lot=bool(cs.get("recalculate_lot", False)),
        lot_mode=lot_mode,
        lot_value=float(cs.get("lot_value", 0.01)),
        lot_ranges=lot_ranges,
        poll_interval=float(cs.get("poll_interval", 0.5)),
        fallback_interval=float(cs.get("fallback_interval", 30.0)),
        max_slippage=int(cs.get("max_slippage", 10)),
        volatility_lot_boost=_parse_volatility_lot_boost(cs.get("volatility_lot_boost")),
        symbol_lot_rules=_parse_symbol_lot_rules(cs.get("symbols")),
    )

    followers: list[FollowerConfig] = []
    for i, acc_data in enumerate(raw_followers):
        account = _parse_account(acc_data, f"Follower {i + 1}")
        raw_cs = acc_data.get("copy_settings")
        follower_cs = _parse_follower_copy_settings(raw_cs, copy) if raw_cs is not None else copy
        followers.append(FollowerConfig(account=account, copy_settings=follower_cs))

    return Settings(master=master, followers=followers, copy=copy)
