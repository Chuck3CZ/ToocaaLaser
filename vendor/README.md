# vendor/

| File | What | Source | Licence |
|---|---|---|---|
| `opencv-4.10.0.js` | OpenCV 4.10.0 compiled to WebAssembly (single file) – used only while calibrating the lid camera lens (ChArUco detection, `calibrateCameraExtended`) | npm [`@techstark/opencv-js@4.10.0-release.1`](https://www.npmjs.com/package/@techstark/opencv-js) `dist/opencv.js` | Apache 2.0, see `OPENCV-LICENSE.txt` |

sha256 `19b46167b59efbef9cc3836264b0b657110833e4b1df84282004b8b3141c048d`

Kept in the repo (not loaded from a CDN) so the calibration works offline and
on networks that block CDNs. The web app loads it lazily, only when you press
"Photo of the board".
