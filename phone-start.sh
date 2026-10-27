#!/data/data/com.termux/files/usr/bin/bash
# Run the existing Kalshi crypto bot continuously on Android/Termux.
# Paper-only by design; do not change these safeguards to enable live trading.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python >/dev/null 2>&1; then
  echo "Python is missing. In Termux run: pkg install python"
  exit 1
fi
if [ ! -d .venv ]; then
  python -m venv .venv
fi
# Termux packages for scientific dependencies are preferable to compiling
# NumPy/SciPy/scikit-learn from pip on Android.
if command -v pkg >/dev/null 2>&1; then
  echo "If imports fail, install Termux packages with:"
  echo "  pkg install python-numpy python-scipy python-scikit-learn python-cryptography"
fi
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
mkdir -p data
export BOT_MODE=paper
export LIVE_TRADING_CONFIRMED=no
export ALIGN_15M_START=yes
export CONTINUOUS_SESSION_MINUTES=525600
export BOT_DB="${BOT_DB:-$PWD/data/bot.sqlite}"

if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock || true
fi
echo "Starting existing Kalshi bot in continuous PAPER mode."
echo "Keep Termux battery optimization disabled for reliable background operation."
exec python -m bot.main continuous
