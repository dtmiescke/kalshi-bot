"""Central configuration. Everything tunable lives here or in environment variables.

Secrets (API keys) are ONLY read from environment variables, never written in code.
"""
import os
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
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
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
}

# Kalshi 15-minute crypto Up/Down series -> Coinbase spot pair.
# KXBTC15M is confirmed; check the ETH/SOL tickers in the Kalshi app.
CRYPTO_15M = {
    "KXBTC15M": "BTC-USD",
    "KXETH15M": "ETH-USD",
    "KXSOL15M": "SOL-USD",
}

# Sports selection is environment/config driven; it does not require UI changes.
# SPORTS_MODE=all discovers the Kalshi sports catalog; manual uses comma-separated tickers.
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
    sports_mode: str = field(default_factory=lambda: _env("SPORTS_MODE", "all").strip().lower())
    sports_selected: str = field(default_factory=lambda: _env("SPORTS_SELECTED", "").strip())
    combos_enabled: bool = field(default_factory=lambda: _env("COMBOS_ENABLED", "no").strip().lower() in ("yes", "true", "1"))
    combo_min_legs: int = field(default_factory=lambda: _env_int("COMBO_MIN_LEGS", 3))
    combo_max_legs: int = field(default_factory=lambda: _env_int("COMBO_MAX_LEGS", 8))
    combo_min_edge: float = field(default_factory=lambda: _env_pct("COMBO_MIN_EDGE", 6))

    def selected_series(self):
        if self.sports_mode == "all":
            return None
        if self.sports_mode != "manual":
            raise ValueError("SPORTS_MODE must be all or manual")
        return {item.strip().upper() for item in self.sports_selected.split(",") if item.strip()}

    # --- data budget --------------------------------------------------------
    # The Odds API free tier is ~500 credits/month. One h2h call per sport costs 1.
    # Cost per call = number of regions. "us" = 1 credit; "us,eu" = 2 credits but
    # adds Pinnacle, the sharpest book. Default keeps a free key under budget.
    odds_regions: str = field(default_factory=lambda: _env("ODDS_REGIONS", "us"))
    odds_refresh_minutes: int = field(
        default_factory=lambda: _env_int("ODDS_REFRESH_MINUTES", 360))

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
    max_per_day: float = field(default_factory=lambda: _env_float("MAX_PER_DAY", 150.0))

    # --- account limits (from the Council Lab design) ----------------------------
    max_open_positions: int = field(default_factory=lambda: _env_int("MAX_OPEN_POSITIONS", 5))
    max_open_cost: float = field(default_factory=lambda: _env_float("MAX_OPEN_COST", 200.0))
    daily_loss_limit: float = field(default_factory=lambda: _env_float("DAILY_LOSS_LIMIT", 30.0))

    # --- crypto 15-minute markets ----------------------------------------------
    crypto_enabled: bool = field(default_factory=lambda: _env("CRYPTO_ENABLED", "yes") == "yes")
    crypto_min_edge: float = field(default_factory=lambda: _env_pct("CRYPTO_MIN_EDGE", 5))
    crypto_max_per_bet: float = field(default_factory=lambda: _env_float("CRYPTO_MAX_PER_BET", 10.0))
    # one window = BTC+ETH+SOL bets that settle together (they move together)
    crypto_max_per_window: float = field(
        default_factory=lambda: _env_float("CRYPTO_MAX_PER_WINDOW", 20.0))
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
        return bool(self.openrouter_key or _env("OMNIROUTE_URL"))

    def base_url(self):
        if self.kalshi_base_url:
            return self.kalshi_base_url.rstrip("/")
        return KALSHI_URLS["production" if self.mode == "live" else "demo"]

    def market_data_url(self):
        """Public market data always comes from production (real prices),
        even in paper mode — that's what we're trying to beat."""
        return _env("KALSHI_DATA_URL", KALSHI_URLS["production"]).rstrip("/")

    def validate(self):
        if self.sports_mode not in ("all", "manual"):
            raise ValueError("SPORTS_MODE must be all/manual")
        if not 3 <= self.combo_min_legs <= 8:
            raise ValueError("COMBO_MIN_LEGS must be between 3 and 8")
        if not self.combo_min_legs <= self.combo_max_legs <= 8:
            raise ValueError("COMBO_MAX_LEGS must be between COMBO_MIN_LEGS and 8")
        if not 0 < self.combo_min_edge < 1:
            raise ValueError("COMBO_MIN_EDGE must be between 0 and 1")
        if self.mode not in ("paper", "demo", "live"):
            raise ValueError(f"BOT_MODE must be paper/demo/live, got {self.mode!r}")
        if self.mode == "live" and not self.live_confirmed:
            raise RuntimeError(
                "Live mode refused: set LIVE_TRADING_CONFIRMED=yes only after demo "
                "results prove a real edge.")
        if self.mode in ("demo", "live") and not (self.kalshi_key_id and self.kalshi_private_key):
            raise RuntimeError("Demo/live mode needs KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY.")
