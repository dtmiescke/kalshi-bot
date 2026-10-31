"""Keep a player's projection close to what he actually averages, and keep overs to lines he can reach.

Three rules, each aimed at the same habit (a forecast that floats well above the player's real average):

* the projection may sit at most UP above his season average (and at most DOWN below it): a hot week or a soft matchup can
  nudge it, not lift it;
* an OVER (YES on "N+") is only a candidate when N is within REACH of his average (or one whole unit above it for small counts
  like steals), so the bot never backs a line he rarely gets near;
* the chance of clearing a line blends his own history with a normal curve whose spread is widened for how few games there
  are, and is never allowed to claim more than 97% or less than 3%.

They narrow what the trackers will pick; they place nothing."""
import math
from statistics import NormalDist, pstdev

UP = 0.10
DOWN = 0.20
REACH = 0.15
CLIP = (0.03, 0.97)
MIN_SEASON_GAMES = 5


def season_average(season_values, all_values):
    """His average this season once he has MIN_SEASON_GAMES, else his last 30 games."""
    pool = season_values if len(season_values) >= MIN_SEASON_GAMES else all_values[-30:]
    return sum(pool) / len(pool) if pool else None


def clamp(mu, avg):
    """The projection held between (1 - DOWN) and (1 + UP) times his average. Returns (mu, held) where held says it was moved."""
    if avg is None or avg <= 0:
        return mu, False
    lo, hi = avg * (1 - DOWN), avg * (1 + UP)
    out = min(hi, max(lo, mu))
    return out, abs(out - mu) > 1e-9


def reachable(line, avg):
    """Is an over on this line one he gets near? Small counts get one whole unit of room."""
    if avg is None:
        return True
    return line <= max(avg * (1 + REACH), avg + 1.0)


def blend_prob(xs, threshold):
    """P(stat above threshold) from his scaled history xs: half his own record, half a normal curve widened for few games."""
    n = len(xs)
    if n < 3:
        return None
    emp = (sum(1 for x in xs if x > threshold) + 0.5) / (n + 1)
    mean = sum(xs) / n
    sd = pstdev(xs) * math.sqrt(1 + 1.0 / n)
    if sd <= 1e-9:
        return min(CLIP[1], max(CLIP[0], emp))
    norm = 1 - NormalDist(mean, sd).cdf(threshold)
    return min(CLIP[1], max(CLIP[0], 0.5 * emp + 0.5 * norm))
