# Vendored third-party code

The dashboard (`index.html`) loads everything it executes from this repository, so it works
without a CDN and the Content-Security-Policy needs no external script host.

## `chart.umd.js` — Chart.js 4.4.1 (MIT)

| | |
|---|---|
| Source | npm package `chart.js@4.4.1`, file `package/dist/chart.umd.js` |
| Tarball | `https://registry.npmjs.org/chart.js/-/chart.js-4.4.1.tgz` (1,216,509 bytes) |
| Tarball integrity (matches the npm registry) | `sha512-C74QN1bxwV1v2PEujhmKjOZ7iUM4w6BWs23Md/6aOZZSlwMzeCIDGuZay++rBgChYru7/+QFeoQW0fQoP534Dg==` |
| Original file | 205,125 bytes, SHA-256 `74401d738dd3e03ee5dfb3b6841210fe2c4ead8a960c4011ca4ba0b78a9fd8f3` |
| cdnjs cross-check | the original equals cdnjs's published `chart.umd.js` (`sha512-ZwR1/gSZM3ai6vCdI+LVF1zSq/5HznD3ZSTk7kajkaj4D292NLuduDCO1c/NT8Id+jE58KYLKT7hXnbtryGmMg==`) |
| **Vendored file** | 205,087 bytes, SHA-256 `0ee28337f25838a7a5d6b1e8b2b02279ab10e80e17c217a99893a7a717f2ba05` |
| SRI used by the page | `sha384-5V9QyFb3nuaog3f4RHnWJM027f1LdHLRTjeDMpRQEnzrc2W+Qa1dqDqu+V68JpaR` |
| License | MIT. The file keeps its own banner (`Chart.js v4.4.1 … Released under the MIT License`); the full text is in `LICENSE-chartjs.md` (from the same package) |

**The one change.** The original ends with the line `//# sourceMappingURL=chart.umd.js.map`.
That line is removed, and nothing else. The source map (953 KB) is not shipped, and without
the line browsers do not request a map file that the server does not have. The change is
reversible: appending exactly `//# sourceMappingURL=chart.umd.js.map` and a newline to the
vendored file reproduces the original npm file byte for byte (a test checks this).

**Why not cdnjs's `chart.umd.min.js`?** It is a separately re-minified build with no license
banner. It exports the same API (same version, controllers, scales, elements and plugins),
but it is not the registry-published artifact, so it was not used.

**Do not edit or reformat these files.** `.gitattributes` marks `vendor/**` as `-text` so Git
never converts line endings; the server refuses to start if the file no longer matches the
`integrity` attribute in `index.html`.

## Updating Chart.js

1. Download the new version from npm and check the tarball against the registry's
   `dist.integrity`.
2. Take `package/dist/chart.umd.js`; remove only the final `sourceMappingURL` line.
3. Replace `chart.umd.js` and `LICENSE-chartjs.md`; update the hashes above.
4. Update the `integrity` attribute in `index.html` (`sha384-` + base64 of the file), and the
   expected values in `tests/api/test_api_dashboard.py`.
5. Run the tests and the browser validation.
