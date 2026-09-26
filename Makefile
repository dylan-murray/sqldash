.PHONY: test lint fmt fmt-check js-test serve-demo build check-install ci-local demo-capture vendor-update

test:
	uv run pytest -q
	node --test tests/*.mjs

js-test:
	node --test tests/*.mjs

lint:
	uv run ruff check sqldash tests

fmt:
	uv run ruff format sqldash tests

fmt-check:
	uv run ruff format --check sqldash tests

serve-demo:
	rm -rf /tmp/sqldash-demo && uv run sqldash init --demo /tmp/sqldash-demo && uv run sqldash serve /tmp/sqldash-demo

build:
	rm -rf dist && uv build

check-install:
	./scripts/check_install.sh

# make ci-local PR=742 [ARGS=--quick|--report]
ci-local:
	./scripts/ci_local.sh $(PR) $(ARGS)

demo-capture:
	uv run python scripts/capture_demo.py

VENDOR := sqldash/static/vendor
vendor-update:
	curl -fsSL -o $(VENDOR)/echarts.min.js https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js
	curl -fsSL -o $(VENDOR)/gridstack-all.js https://cdn.jsdelivr.net/npm/gridstack@11.5.1/dist/gridstack-all.js
	curl -fsSL -o $(VENDOR)/gridstack.min.css https://cdn.jsdelivr.net/npm/gridstack@11.5.1/dist/gridstack.min.css
	curl -fsSL -o $(VENDOR)/geist-var.woff2 "https://cdn.jsdelivr.net/npm/@fontsource-variable/geist@5.2.5/files/geist-latin-wght-normal.woff2"
	curl -fsSL -o $(VENDOR)/geist-mono-var.woff2 "https://cdn.jsdelivr.net/npm/@fontsource-variable/geist-mono@5.2.5/files/geist-mono-latin-wght-normal.woff2"
	for f in ace.js mode-sql.js theme-tomorrow.js theme-tomorrow_night.js ext-language_tools.js; do \
		curl -fsSL -o $(VENDOR)/ace/$$f https://cdn.jsdelivr.net/npm/ace-builds@1.36.5/src-min-noconflict/$$f; \
	done
