"""Typed configuration: config.yaml for parameters, .env for secrets."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Code lives at CODE_ROOT. Config, .env and the database normally sit beside it,
# but LUMBUNG_HOME moves them elsewhere -- which is what lets a second person run
# their own instance off this same install: their own .env, their own holdings,
# their own database, their own Indodax key. Nothing about the strategy changes;
# only whose numbers it reads and whose money it trades.
CODE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = Path(os.environ.get("LUMBUNG_HOME") or CODE_ROOT).resolve()

# Values shipped in .env.example. Treated as "not set".
PLACEHOLDERS = frozenset({"your_api_key_here", "your_api_secret_here", "", "changeme"})


class Secrets(BaseSettings):
    """Secrets from .env. Never logged, never written to the journal."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    indodax_key: SecretStr = SecretStr("")
    indodax_secret: SecretStr = SecretStr("")
    # Cloudflare Access. With these set, the dashboard trusts Access's signed
    # identity instead of asking for a bearer token -- you are already logged in
    # by the time the request arrives, so asking again is friction with no gain.
    # ACCESS_EMAILS is a second, independent allowlist: an Access policy mistake
    # should not silently become full access to a trading dashboard.
    access_team_domain: str = ""
    access_aud: str = ""
    access_emails: str = ""
    dashboard_token: SecretStr = SecretStr("")
    indodax_whitelist_ip: str = ""
    openrouter_api_key: SecretStr = SecretStr("")
    # Self-hosted OpenAI-compatible endpoint, used only for receipt OCR.
    local_llm_url: str = ""
    local_llm_model: str = ""
    local_llm_key: SecretStr = SecretStr("")
    ta_mode: str = "paper"
    # ── OpenCode 2 (GLM): the always-on answerer, and later the engine brain.
    # Empty opencode_model keeps every agent feature off -- the deterministic
    # behaviour is unchanged, which is what a second profile on this install
    # relies on.
    opencode_bin: str = ""            # "" resolves to "opencode2" on PATH
    opencode_port: int = 42778        # the Telegram bot claims 42777; never share
    opencode_model: str = ""          # e.g. zai-coding-plan/glm-5.3
    agent_worker: str = ""            # "1" runs the always-on chat/research answerer
    agent_brain: str = "donchian"     # engine signal source: donchian | llm

    @property
    def has_indodax(self) -> bool:
        key = self.indodax_key.get_secret_value().strip()
        sec = self.indodax_secret.get_secret_value().strip()
        # The .env.example placeholders are non-empty strings, so a plain
        # truthiness test reports "credentials present" for an untouched copy --
        # which makes `doctor` lie and live mode fail with a confusing 401.
        return bool(key and sec and key not in PLACEHOLDERS and sec not in PLACEHOLDERS)


class CapitalCfg(BaseModel):
    sleeve_idr: float


class UniverseCfg(BaseModel):
    pairs: list[str]
    timeframe: str = "60"


class StrategyCfg(BaseModel):
    name: str = "donchian_trend"
    donchian_lookback: int = 55
    ema_fast: int = 50
    ema_slow: int = 200
    adx_period: int = 14
    adx_min: float = 25.0
    atr_period: int = 14
    stop_atr_mult: float = 4.0
    trail_atr_mult: float = 4.5
    partial_tp_r: float = 999.0
    partial_tp_frac: float = 0.5


class RiskCfg(BaseModel):
    risk_per_trade_pct: float = 0.01
    max_position_pct: float = 0.25
    max_concurrent_positions: int = 6
    max_total_exposure_pct: float = 0.75
    daily_loss_limit_pct: float = 0.03
    max_drawdown_pct: float = 0.20
    min_notional_idr: float = 50_000


class CostsCfg(BaseModel):
    maker_fee_pct: float = 0.001
    taker_fee_pct: float = 0.002
    sell_tax_pct: float = 0.0021
    safety_margin_pct: float = 0.0005
    slippage_ticks: int = 1
    # A market order crosses the book, so it pays roughly half the spread on top
    # of the taker fee. Post-only orders rest AT the bid/ask and pay none of it.
    # Live spreads range 0.02% (BTC) to >1% (SHIB); this is a blended default.
    taker_spread_pct: float = 0.0025

    def buy_cost_pct(self, *, taker: bool) -> float:
        """Fraction of notional lost on a buy."""
        if taker:
            return self.taker_fee_pct + self.taker_spread_pct + self.safety_margin_pct
        return self.maker_fee_pct + self.safety_margin_pct

    def sell_cost_pct(self, *, taker: bool) -> float:
        """Fraction of notional lost on a sell, including the 0.21% PPh final."""
        base = self.sell_tax_pct + self.safety_margin_pct
        if taker:
            return self.taker_fee_pct + self.taker_spread_pct + base
        return self.maker_fee_pct + base

    @property
    def round_trip_maker_pct(self) -> float:
        return self.buy_cost_pct(taker=False) + self.sell_cost_pct(taker=False)


class ExecutionCfg(BaseModel):
    poll_interval_sec: int = 20
    entry_chase_max: int = 3
    entry_chase_wait_sec: int = 60
    entry_max_slip_pct: float = 0.003
    exit_timeout_sec: int = 900
    heartbeat_stale_sec: int = 300


class StocksCfg(BaseModel):
    enabled: bool = True
    # Ceiling for ONE stock as a share of NET WORTH before the concentration
    # rule fires.
    #
    # 0.20 is the outer edge of what planners call overconcentrated: the common
    # consensus is 5-10% per position, red flags start at 10-20%, and 30%+ is
    # described as "a dominant planning issue". The previous 0.40 was looser
    # than any published guidance and let a 48% position read as only mildly
    # over -- the rule existed but its threshold made it almost unreachable.
    max_position_pct: float = 0.20
    # Final PPh withheld on dividends paid to an Indonesian individual resident.
    # 10% is the default case; it is 0 when the dividend is reinvested in
    # Indonesia for three years, which is a real and common election. Set it to
    # what actually reaches you -- every other income figure here is net, and a
    # gross one mixed in makes stocks look better than bonds by exactly this.
    dividend_tax_pct: float = 0.10
    budget_idr: float = 2_000_000
    run_at_wib: str = "16:15"
    donchian_lookback: int = 20
    ema_fast: int = 50
    ema_slow: int = 200
    adx_min: float = 20.0
    atr_period: int = 14
    stop_atr_mult: float = 2.0
    tp_atr_mult: float = 4.0
    fee_buy_pct: float = 0.0015
    fee_sell_pct: float = 0.0025


class NewsCfg(BaseModel):
    enabled: bool = True
    days: int = 7
    # Any OpenRouter model id. Cheap and fast is right here -- the task is
    # classification, not writing. Without a key it falls back to keywords.
    model: str = "anthropic/claude-3.5-haiku"


class PathsCfg(BaseModel):
    db: str = "data/lumbung.db"
    halt_file: str = "HALT"
    log_dir: str = "logs"


class Config(BaseModel):
    capital: CapitalCfg
    universe: UniverseCfg
    strategy: StrategyCfg = Field(default_factory=StrategyCfg)
    risk: RiskCfg = Field(default_factory=RiskCfg)
    costs: CostsCfg = Field(default_factory=CostsCfg)
    execution: ExecutionCfg = Field(default_factory=ExecutionCfg)
    stocks: StocksCfg = Field(default_factory=StocksCfg)
    news: NewsCfg = Field(default_factory=NewsCfg)
    paths: PathsCfg = Field(default_factory=PathsCfg)

    # Resolved absolute paths -------------------------------------------------
    @property
    def db_path(self) -> Path:
        return _abs(self.paths.db)

    @property
    def data_dir(self) -> Path:
        """Where the journal, the chat transcript and the queues live.

        The database has always been the anchor for this directory; naming it
        stops every caller from spelling `db_path.parent` and getting it subtly
        wrong once.
        """
        return self.db_path.parent

    @property
    def halt_path(self) -> Path:
        return _abs(self.paths.halt_file)

    @property
    def log_path(self) -> Path:
        return _abs(self.paths.log_dir)


def _abs(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_config(path: str | Path | None = None) -> Config:
    cfg_path = Path(path) if path else PROJECT_ROOT / "config" / "config.yaml"
    with open(cfg_path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return Config.model_validate(raw)


def load_watchlist(path: str | Path | None = None) -> list[str]:
    wl_path = Path(path) if path else PROJECT_ROOT / "config" / "stocks.yaml"
    with open(wl_path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return list(raw.get("watchlist", []))


@lru_cache(maxsize=1)
def get_secrets() -> Secrets:
    return Secrets()


def env_mode() -> str:
    return os.getenv("TA_MODE", get_secrets().ta_mode).lower()
