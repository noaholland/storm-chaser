PY := .venv/bin/python
HOST ?= 0.0.0.0
PORT ?= 8000

.PHONY: install test demo serve data live-check web app e2e video site parity e2e-static

install:
	uv venv -q --allow-existing && uv pip install -q -e '.[dev]'

test:
	$(PY) -m pytest -q

demo:
	$(PY) scripts/e2e.py

serve:
	$(PY) -m uvicorn stormchaser.api:app --host $(HOST) --port $(PORT)

# Build the dashboard into web/dist (served by the API under /ui/). Without npm, an existing build is reused.
web:
	@if command -v npm >/dev/null 2>&1; then \
		cd web && npm install --no-audit --no-fund --loglevel=error && npm run build --silent; \
	elif [ -f web/dist/index.html ]; then \
		echo "npm not found: serving the existing dashboard build"; \
	else \
		echo "npm (Node 20+) is needed to build the dashboard; the API alone runs with 'make serve'"; exit 1; \
	fi

# One command for the whole thing: API + dashboard on http://localhost:$(PORT)/
app: web
	@echo "Storm Chaser dashboard: http://localhost:$(PORT)/  (API docs: /docs)"
	@exec $(PY) -m uvicorn stormchaser.api:app --host $(HOST) --port $(PORT)

# Browser end-to-end tests; they start their own stack with 'make app' on port 8787.
e2e:
	cd web && npm install --no-audit --no-fund --loglevel=error && npx playwright install chromium && npx playwright test

# The backend-free demo site in docs/ (GitHub Pages): the dashboard built in static mode, plus the bundled
# replays recorded from the Python engine into docs/data/. No server needed to view it:
#   python3 -m http.server -d docs 8080
site:
	cd web && npm install --no-audit --no-fund --loglevel=error && npm run build:static --silent
	$(PY) scripts/build_static.py docs/data

# Proof that the site serves what the engine computes: the Python stack writes reference output into .parity/,
# then the code the site ships answers the same questions from docs/data/ and every value is compared.
parity:
	$(PY) scripts/parity_expected.py .parity
	cd web && node scripts/parity.mjs ../docs/data ../.parity

# Browser tests of the built static site, served from a sub-path the way GitHub Pages serves it.
e2e-static:
	cd web && npm install --no-audit --no-fund --loglevel=error && npx playwright install chromium && npx playwright test -c playwright.static.config.ts

# Re-record assets/demo.mp4 and assets/demo-short.mp4 (needs ffmpeg; starts its own backend on port 8790).
video: web
	cd web && node scripts/record-demo.mjs

data:
	$(PY) scripts/fetch_data.py

live-check:
	$(PY) scripts/live_check.py
