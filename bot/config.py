"""Central configuration. Everything tunable lives here or in environment variables.

Secrets (API keys) are ONLY read from environment variables, never written in code.
"""
import os
import math
from dataclasses import dataclass, field


def _env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _env_float(name, default):
    return float(_env(name, default))


def _env_pct(name, default):
    """Percent settings: write 3 for 3% (0.03 also works)."""
    v = float(_env(name, default))
    return v / 100 if v > 1 else v


def _env_int(name, default):
    return int(_env(name, default))


# Kalshi base URLs. Check these against Kalshi's official docs before going live;
# third-party guides disagree, so they are overridable.
KALSHI_URLS = {
    "demo": "https://external-api.demo.kalshi.co/trade-api/v2",
    "production": "https://api.elections.kalshi.com/trade-api/v2",
}

# Kalshi sports series -> The Odds API sport key. Verify series tickers in Kalshi's
# docs/app; add more leagues here and the whole pipeline picks them up.
SPORTS = {
    "KXNFLGAME": "americanfootball_nfl",
    "KXNBAGAME": "basketball_nba",
    "KXMLBGAME": "baseball_mlb",
    "KXNHLGAME": "icehockey_nhl",
    "KXNCAAFGAME": "americanfootball_ncaaf",
    "KXEPLGAME": "soccer_epl",
    "KXLALIGAGAME": "soccer_spain_la_liga",
    "KXBUNDESLIGAGAME": "soccer_germany_bundesliga",
    "KXSERIEAGAME": "soccer_italy_serie_a",
    "KXLIGUE1GAME": "soccer_france_ligue_one",
    "KXUCLGAME": "soccer_uefa_champs_league",
    "KXUELGAME": "soccer_uefa_europa_league",
    "KXUECLGAME": "soccer_uefa_europa_conference_league",
    "KXEREDIVISIEGAME": "soccer_netherlands_eredivisie",
    "KXLIGAMXGAME": "soccer_mexico_ligamx",
    "KXBRASILEIROGAME": "soccer_brazil_campeonato",
    "KXSAUDIPLGAME": "soccer_saudi_professional_league",
}

# Kalshi 15-minute crypto Up/Down series -> Coinbase spot pair.
# KXBTC15M is confirmed; check the ETH/SOL tickers in the Kalshi app.
CRYPTO_15M = {
    "KXBTC15M": "BTC-USD",
    "KXETH15M": "ETH-USD",
    "KXSOL15M": "SOL-USD",
}

# Sports selection is environment/config driven; it does not require UI changes.
# configured (default) = the five currently configured leagues; manual = explicit tickers;
# all = discover every sports series available in Kalshi's catalog.
# COMBOS_ENABLED stays OFF until a separate supported-combo execution path is validated.

# Sharper books get more weight in the consensus. Unknown books get weight 1.0.
BOOK_WEIGHTS = {
    "pinnacle": 3.0,
    "betonlineag": 1.5,
    "lowvig": 1.5,
    "circasports": 2.5,
    "draftkings": 1.0,
    "fanduel": 1.0,
    "betmgm": 0.8,
    "caesars": 0.8,
}


@dataclass
class Settings:
    # --- mode -------------------------------------------------------------
    # paper  = simulated fills against the live Kalshi order book, nothing sent
    # demo   = real orders on Kalshi's demo exchange (play money)
    # live   = real money. Also requires LIVE_TRADING_CONFIRMED=yes
    mode: str = field(default_factory=lambda: _env("BOT_MODE", "paper"))
    live_confirmed: bool = field(
        default_factory=lambda: _env("LIVE_TRADING_CONFIRMED", "no") == "yes")

    # --- credentials (env only) ------------------------------------------
    # TRADING key: demo key in demo mode, real key in live mode
    kalshi_key_id: str = field(default_factory=lambda: _env("KALSHI_API_KEY_ID"))
    kalshi_private_key: str = field(default_factory=lambda: _env("KALSHI_PRIVATE_KEY"))
    kalshi_base_url: str = field(default_factory=lambda: _env("KALSHI_BASE_URL"))
    odds_api_key: str = field(default_factory=lambda: _env("ODDS_API_KEY"))
    openrouter_key: str = field(default_factory=lambda: _env("OPENROUTER_API_KEY"))
    openrouter_model: str = field(default_factory=lambda: _env(
        "OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free"))

    # --- storage ------------------------------------------------------------
    db_path: str = field(default_factory=lambda: _env("BOT_DB", "data/bot.sqlite"))

    # --- sports intelligence and selection ---------------------------------
    sports_mode: str = field(default_factory=lambda: _env("SPORTS_MODE", "configured").strip().lower())
    sports_selected: str = field(default_factory=lambda: (
        os.environ["SPORTS_SELECTED"] if "SPORTS_SELECTED" in os.environ
        else ",".join(SPORTS.keys())).strip())
    # Research is on by default; order execution remains a separate, disabled capability.
    combo_research_enabled: bool = field(default_factory=lambda: _env("COMBO_RESEARCH_ENABLED", "yes").strip().lower() in ("yes", "true", "1"))
    combos_enabled: bool = field(default_factory=lambda: _env("COMBOS_ENABLED", "no").strip().lower() in ("yes", "true", "1"))
    combo_min_legs: int = field(default_factory=lambda: _env_int("COMBO_MIN_LEGS", 3))
    combo_max_legs: int = field(default_factory=lambda: _env_int("COMBO_MAX_LEGS", 8))
    combo_min_edge: float = field(default_factory=lambda: _env_pct("COMBO_MIN_EDGE", 6))
    combo_audit_lead_seconds: int = field(default_factory=lambda: _env_int("COMBO_AUDIT_LEAD_SECONDS", 300))

    def selected_series(self):
        if self.sports_mode == "all":
            return None
        if self.sports_mode == "configured":
            # Keep the original five leagues as the default; the wider SPORTS
            # catalog is opt-in through manual/all mode.
            return {"KXNFLGAME", "KXNCAAFGAME", "KXNBAGAME", "KXMLBGAME", "KXNHLGAME"}
        if self.sports_mode != "manual":
            raise ValueError("SPORTS_MODE must be configured, manual, or all")
        return {item.strip().upper() for item in self.sports_selected.split(",") if item.strip()}

    # --- data budget --------------------------------------------------------
    # The Odds API free tier is ~500 credits/month. One h2h call per sport costs 1.
    # Cost per call = number of regions. "us" = 1 credit; "us,eu" = 2 credits but
    # adds Pinnacle, the sharpest book. Default keeps a free key under budget.
    odds_regions: str = field(default_factory=lambda: _env("ODDS_REGIONS", "us"))
    odds_refresh_minutes: int = field(
        default_factory=lambda: _env_int("ODDS_REFRESH_MINUTES", 240))
    odds_max_age_minutes: float = field(
        default_factory=lambda: _env_float("ODDS_MAX_AGE_MINUTES", 60))

    # Optional paper-only aggression. No effect on demo/live execution.
    aggression_level: str = field(default_factory=lambda: _env("AGGRESSION_LEVEL", "aggressive").strip().lower())

    # --- decision rules -----------------------------------------------------
    min_edge: float = field(default_factory=lambda: _env_pct("MIN_EDGE", 3))
    min_confidence: float = field(default_factory=lambda: _env_pct("MIN_CONFIDENCE", 55))
    min_price: float = 0.05   # never buy dust contracts
    max_price: float = 0.95   # never pay 96c+ to win 4c
    kelly_fraction: float = field(default_factory=lambda: _env_float("KELLY_FRACTION", 0.25))

    # --- risk caps (dollars) ------------------------------------------------
    bankroll: float = field(default_factory=lambda: _env_float("BANKROLL", 1000.0))
    max_per_bet: float = field(default_factory=lambda: _env_float("MAX_PER_BET", 25.0))
    max_per_game: float = field(default_factory=lambda: _env_float("MAX_PER_GAME", 50.0))
    max_per_day: float = field(default_factory=lambda: _env_float("MAX_PER_DAY", 20000.0))

    # --- account limits (from the Council Lab design) ----------------------------
    max_open_positions: int = field(default_factory=lambda: _env_int("MAX_OPEN_POSITIONS", 5))
    max_open_cost: float = field(default_factory=lambda: _env_float("MAX_OPEN_COST", 200.0))
    daily_loss_limit: float = field(default_factory=lambda: _env_float("DAILY_LOSS_LIMIT", 20000.0))

    # --- council (DON_THEO_SPEC): members start advisory and EARN power ---------
    # Veto: >= COUNCIL_VETO_MIN_VOTES graded votes with a Brier better than the
    # market's on the same contracts. Full vote: more votes and a clear margin.
    # Judged on each member's last COUNCIL_POWER_WINDOW graded votes, so a member
    # whose record drops is demoted. Doghouse: <= COUNCIL_DOGHOUSE_MAX_RIGHT right
    # in the last COUNCIL_DOGHOUSE_WINDOW (doghouse members lose their power).
    council_veto_min_votes: int = field(default_factory=lambda: _env_int("COUNCIL_VETO_MIN_VOTES", 100))
    council_full_min_votes: int = field(default_factory=lambda: _env_int("COUNCIL_FULL_MIN_VOTES", 300))
    council_full_margin: float = field(default_factory=lambda: _env_float("COUNCIL_FULL_MARGIN", 0.005))
    council_power_window: int = field(default_factory=lambda: _env_int("COUNCIL_POWER_WINDOW", 300))
    council_doghouse_window: int = field(default_factory=lambda: _env_int("COUNCIL_DOGHOUSE_WINDOW", 9))
    council_doghouse_max_right: int = field(
        default_factory=lambda: _env_int("COUNCIL_DOGHOUSE_MAX_RIGHT", 2))

    # --- crypto 15-minute markets ----------------------------------------------
    crypto_enabled: bool = field(default_factory=lambda: _env("CRYPTO_ENABLED", "yes") == "yes")
    crypto_min_edge: float = field(default_factory=lambda: _env_pct("CRYPTO_MIN_EDGE", 5))
    crypto_min_bet: float = field(default_factory=lambda: _env_float("CRYPTO_MIN_BET", 15.0))
    crypto_max_per_bet: float = field(default_factory=lambda: _env_float("CRYPTO_MAX_PER_BET", 20.0))
    # one window = BTC+ETH+SOL bets that settle together (they move together)
    crypto_max_per_window: float = field(
        default_factory=lambda: _env_float("CRYPTO_MAX_PER_WINDOW", 20.0))
    # Crypto sides priced below this (dollars) are long shots: bought only when the
    # raw exact-contract model, not just the blend, also shows the edge. 0 disables.
    crypto_longshot_price: float = field(
        default_factory=lambda: _env_float("CRYPTO_LONGSHOT_PRICE", 0.20))
    crypto_min_minutes: float = 2.0    # too close to the bell: our price feed lags
    crypto_max_minutes: float = 13.0   # too early: strike may not be set yet

    # --- execution ---------------------------------------------------------------
    # FIXED_BET: dollars per bet you choose (still capped by the limits above).
    # Leave empty to size by edge (fractional Kelly).
    fixed_bet: float = field(default_factory=lambda: float(_env("FIXED_BET", 0) or 0))
    # taker = buy at the ask now; maker = rest a limit order at a better price
    # (live/burst mode only; falls back to taker in snapshot runs)
    order_style: str = field(default_factory=lambda: _env("ORDER_STYLE", "maker"))
    maker_fee_rate: float = field(default_factory=lambda: _env_float("MAKER_FEE_RATE", 0.0175))
    maker_wait_s: int = field(default_factory=lambda: _env_int("MAKER_WAIT_SECONDS", 45))
    # sell early when the bid is worth more than holding (by this margin)
    exit_margin: float = field(default_factory=lambda: _env_pct("EXIT_MARGIN", 2))
    exits_enabled: bool = field(default_factory=lambda: _env("EXITS_ENABLED", "yes") == "yes")

    # --- fees ---------------------------------------------------------------
    # Kalshi's general taker fee is roughly ceil(rate * C * P * (1-P)) dollars.
    # Some markets use a different rate; check Kalshi's fee schedule.
    taker_fee_rate: float = field(default_factory=lambda: _env_float("TAKER_FEE_RATE", 0.07))

    # DATA key: a key from your REAL Kalshi account, used only to read the live
    # WebSocket price feed (works in paper mode, no deposit needed, never trades).
    # Falls back to the trading key in live mode, where that key is a real one.
    def data_key(self):
        kid = _env("KALSHI_DATA_KEY_ID") or (self.kalshi_key_id if self.mode == "live" else None)
        pem = _env("KALSHI_DATA_PRIVATE_KEY") or (self.kalshi_private_key if self.mode == "live" else None)
        return (kid, pem) if kid and pem else (None, None)

    def ai_enabled(self):
        return bool(self.openrouter_key or _env("OMNIROUTE_URL") or _env("LITELLM_URL"))

    def base_url(self):
        if self.kalshi_base_url:
            return self.kalshi_base_url.rstrip("/")
        return KALSHI_URLS["production" if self.mode == "live" else "demo"]

    def market_data_url(self):
        """Public market data always comes from production (real prices),
        even in paper mode — that's what we're trying to beat."""
        return _env("KALSHI_DATA_URL", KALSHI_URLS["production"]).rstrip("/")

    def validate(self):
        # Validate scheduler environment before any market work or execution.
        # BURST_MINUTES=0 is intentional snapshot-only mode.
        float_settings = {
            "SPORTS_EVERY_HOURS": (5.5, False),
            "BURST_MINUTES": (8.0, True),
            "TRAIN_EVERY_HOURS": (24.0, False),
            "TRAIN_RETRY_HOURS": (6.0, False),
        }
        for name, (default, allow_zero) in float_settings.items():
            raw = os.environ.get(name, str(default))
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                requirement = "non-negative" if allow_zero else "positive"
                raise ValueError(f"{name} must be a finite {requirement} number") from exc
            if not math.isfinite(value) or value < 0 or (not allow_zero and value == 0):
                requirement = "non-negative" if allow_zero else "positive"
                raise ValueError(f"{name} must be a finite {requirement} number")
        integer_settings = {
            "ML_MIN_WINDOWS": 300,
            "SPORTS_SCAN_EVENT_BUDGET": 150,
            "SPORTS_INTEL_EVENT_BUDGET": 6,
            "SPORTS_AI_NEWS_REFRESH_MINUTES": 30,
        }
        for name, default in integer_settings.items():
            raw = os.environ.get(name, str(default))
            try:
                value = int(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be a positive integer") from exc
            if value <= 0:
                raise ValueError(f"{name} must be a positive integer")

        if self.sports_mode not in ("configured", "all", "manual"):
            raise ValueError("SPORTS_MODE must be configured/all/manual")
        if self.sports_mode == "manual" and not self.selected_series():
            raise ValueError("SPORTS_MODE=manual requires at least one SPORTS_SELECTED series ticker")
        if not 3 <= self.combo_min_legs <= 8:
            raise ValueError("COMBO_MIN_LEGS must be between 3 and 8")
        if not self.combo_min_legs <= self.combo_max_legs <= 8:
            raise ValueError("COMBO_MAX_LEGS must be between COMBO_MIN_LEGS and 8")
        if self.combo_audit_lead_seconds < 0:
            raise ValueError("COMBO_AUDIT_LEAD_SECONDS must be non-negative")
        # A zero/negative odds refresh interval can hammer the free API budget;
        # a nonpositive maker wait can bypass the intended order reconciliation window.
        if self.odds_refresh_minutes <= 0:
            raise ValueError("ODDS_REFRESH_MINUTES must be positive")
        if not math.isfinite(self.odds_max_age_minutes) or self.odds_max_age_minutes <= 0:
            raise ValueError("ODDS_MAX_AGE_MINUTES must be a finite positive number")
        if self.maker_wait_s <= 0:
            raise ValueError("MAKER_WAIT_SECONDS must be positive")
        if self.combos_enabled:
            raise ValueError(
                "Combo order execution is not implemented; keep COMBOS_ENABLED=no. "
                "Read-only combo research remains available.")
        if not 3 <= self.combo_min_legs <= 8:
            raise ValueError("COMBO_MIN_LEGS must be between 3 and 8")
        if not self.combo_min_legs <= self.combo_max_legs <= 8:
            raise ValueError("COMBO_MAX_LEGS must be between COMBO_MIN_LEGS and 8")
        if not 0 < self.combo_min_edge < 1:
            raise ValueError("COMBO_MIN_EDGE must be between 0 and 1")
        if self.aggression_level not in ("off", "conservative", "balanced", "aggressive", "adaptive"):
            raise ValueError("AGGRESSION_LEVEL must be off/conservative/balanced/aggressive/adaptive")
        if self.mode not in ("paper", "demo", "live"):
            raise ValueError(f"BOT_MODE must be paper/demo/live, got {self.mode!r}")
        if self.mode == "live" and not self.live_confirmed:
            raise RuntimeError(
                "Live mode refused: set LIVE_TRADING_CONFIRMED=yes only after demo "
                "results prove a real edge.")
        if self.mode == "live" and _env("LIVE_TRADING_OPERATIONAL_APPROVED", "no") != "yes":
            raise RuntimeError(
                "Live mode refused: operational readiness has not been approved. "
                "Keep BOT_MODE=paper until all live-readiness checks pass.")
        if self.mode in ("demo", "live") and not (self.kalshi_key_id and self.kalshi_private_key):
            raise RuntimeError("Demo/live mode needs KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY.")

        # Validate numeric risk settings before any scheduler or execution path
        # can use them. NaN/inf bypass ordinary comparisons and must be rejected.
        nonnegative = {
            "MAX_PER_BET": self.max_per_bet,
            "MAX_PER_GAME": self.max_per_game,
            "MAX_PER_DAY": self.max_per_day,
            "MAX_OPEN_COST": self.max_open_cost,
            "DAILY_LOSS_LIMIT": self.daily_loss_limit,
            "CRYPTO_MIN_BET": self.crypto_min_bet,
            "CRYPTO_MAX_PER_BET": self.crypto_max_per_bet,
            "CRYPTO_MAX_PER_WINDOW": self.crypto_max_per_window,
            "FIXED_BET": self.fixed_bet,
        }
        for name, value in nonnegative.items():
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative dollar amount")
        if not math.isfinite(self.bankroll) or self.bankroll <= 0:
            raise ValueError("BANKROLL must be a finite positive dollar amount")
        if self.max_open_positions < 0:
            raise ValueError("MAX_OPEN_POSITIONS must be non-negative")
        if not math.isfinite(self.kelly_fraction) or not 0 <= self.kelly_fraction <= 1:
            raise ValueError("KELLY_FRACTION must be between 0 and 1")
        for name, value in (("MIN_EDGE", self.min_edge),
                            ("CRYPTO_MIN_EDGE", self.crypto_min_edge)):
            if not math.isfinite(value) or not 0 < value < 1:
                raise ValueError(f"{name} must be greater than 0 and less than 1")
        if not math.isfinite(self.crypto_longshot_price) or not 0 <= self.crypto_longshot_price < 0.5:
            raise ValueError("CRYPTO_LONGSHOT_PRICE must be a dollar price from 0 up to 0.50")
        if not 1 <= self.council_veto_min_votes <= self.council_full_min_votes:
            raise ValueError("COUNCIL_VETO_MIN_VOTES must be at least 1 and at most COUNCIL_FULL_MIN_VOTES")
        if self.council_power_window < self.council_full_min_votes:
            raise ValueError("COUNCIL_POWER_WINDOW must be at least COUNCIL_FULL_MIN_VOTES")
        if not math.isfinite(self.council_full_margin) or self.council_full_margin < 0:
            raise ValueError("COUNCIL_FULL_MARGIN must be a non-negative Brier margin")
        if not 0 <= self.council_doghouse_max_right < self.council_doghouse_window:
            raise ValueError("COUNCIL_DOGHOUSE_MAX_RIGHT must be below COUNCIL_DOGHOUSE_WINDOW")
        if not math.isfinite(self.min_confidence) or not 0 < self.min_confidence <= 1:
            raise ValueError("MIN_CONFIDENCE must be greater than 0 and at most 1")
        for name, value in (("TAKER_FEE_RATE", self.taker_fee_rate),
                            ("MAKER_FEE_RATE", self.maker_fee_rate)):
            if not math.isfinite(value) or not 0 < value <= 1:
                raise ValueError(f"{name} must be greater than 0 and at most 1")
        if not math.isfinite(self.exit_margin) or not 0 <= self.exit_margin < 1:
            raise ValueError("EXIT_MARGIN must be between 0 and 1")
        if self.order_style not in ("maker", "taker"):
            raise ValueError("ORDER_STYLE must be maker or taker")
