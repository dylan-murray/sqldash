# Vendored assets

All third-party JS/CSS is vendored here (checked into git, shipped in the wheel) so
installs never need a JS toolchain or the network. Update with `make vendor-update`.

| Asset | Version | Source |
|---|---|---|
| echarts.min.js | 5.5.1 | https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js |
| gridstack-all.js / gridstack.min.css | 11.5.1 | https://cdn.jsdelivr.net/npm/gridstack@11.5.1/dist/ |
| ace/* | 1.36.5 | https://cdn.jsdelivr.net/npm/ace-builds@1.36.5/src-min-noconflict/ |
| geist-var.woff2 | 5.2.5 | https://cdn.jsdelivr.net/npm/@fontsource-variable/geist@5.2.5/files/geist-latin-wght-normal.woff2 |
| geist-mono-var.woff2 | 5.2.5 | https://cdn.jsdelivr.net/npm/@fontsource-variable/geist-mono@5.2.5/files/geist-mono-latin-wght-normal.woff2 |
