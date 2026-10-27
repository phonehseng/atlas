"""Optional Python packages fetched as pinned wheels and unpacked, without pip.

Used by the media index (index/deps.py), the assistant (assistant/installer.py) and AirPlay casting (cast/).
Every wheel is pinned to an exact files.pythonhosted.org URL, size and SHA-256 (CPython 3.12, Windows x64: the packaged app's Python) and unpacked,
.dist-info included, into %APPDATA%\\Marquee\\ai\\py312, which is appended to sys.path. That works the same in the
packaged app (no pip there) and from source. A wheel is skipped when its module is importable already: from the
environment (the dev venv), from the packaged app itself (PyInstaller bundles a few shared ones, e.g. packaging,
yaml, click) or from an earlier install, so the index and the assistant share numpy and tokenizers.
"""
import hashlib
import importlib
import importlib.util
import os
import shutil
import sys
import threading
import zipfile
from pathlib import Path

import requests

from . import settings

LIB_DIR = settings.DIR / "ai" / f"py{sys.version_info.major}{sys.version_info.minor}"
DOWNLOADS = LIB_DIR.parent / "downloads"

_P = "https://files.pythonhosted.org/packages/"
# name -> pinned wheel. "module": importable when present; "path": a folder (with bin\) under a sys.path entry.
WHEELS = {
    "faster-whisper": {"version": "1.2.1", "module": "faster_whisper", "licence": "MIT", "size": 1118909,
        "sha256": "79a66ad50688c0b794dd501dc340a736992a6342f7f95e5811be60b5224a26a7",
        "url": _P + "05/99/49ee85903dee060d9f08297b4a342e5e0bcfca2f027a07b4ee0a38ab13f9/"
               "faster_whisper-1.2.1-py3-none-any.whl"},
    "ctranslate2": {"version": "4.8.4", "module": "ctranslate2", "licence": "MIT", "size": 20371102,
        "sha256": "056db4b4c2a31b7811d453155a3d6ab316ddb1dc104f413beee7bce26bcbf92d",
        "url": _P + "af/25/cca7f6807d37cbfd9eebed8bbf136f5214c31d7d66de267ad05b676e9deb/"
               "ctranslate2-4.8.4-cp312-cp312-win_amd64.whl"},
    "av": {"version": "19.0.2", "module": "av", "licence": "BSD-3-Clause", "size": 28150949,
        "sha256": "3ce5a1d7b916d13c16ac58f1756f0fee4af2d53f02bc1403e18550b50d375a22",
        "url": _P + "32/5a/743a83666ded197df8b9957e2800bef9a39d52b23be2ca211bbd14b5fb22/"
               "av-19.0.2-cp312-abi3-win_amd64.whl"},
    "tokenizers": {"version": "0.23.3", "module": "tokenizers", "licence": "Apache-2.0", "size": 2872101,
        "sha256": "1f0823bb00c5fdc98e487354d54dd55a03848d61a1a0bf29a68c77f24f3b26c3",
        "url": _P + "d1/6c/24f074c9a0efb98e61b20aafe6b2641922d5db24e447d5d6daffd9e17555/"
               "tokenizers-0.23.3-cp310-abi3-win_amd64.whl"},
    "huggingface-hub": {"version": "2.3.0", "module": "huggingface_hub", "licence": "Apache-2.0", "size": 855161,
        "sha256": "5da369600233568a126eb5578754935b2add9446b42d9e2fc0927d56840ac1a5",
        "url": _P + "c7/89/5f6ef1162d0323f167b6baab04d1b4bd6a946161a8af6d86f622d9d79aa1/"
               "huggingface_hub-2.3.0-py3-none-any.whl"},
    "hf-xet": {"version": "1.7.0", "module": "hf_xet", "licence": "Apache-2.0", "size": 3822644,
        "sha256": "2b7bb5727889b0f2436dbaaad8fc4c3e66b8240d992716989e0c086b4278b1bc",
        "url": _P + "4d/59/b35106dfa71b6eef605dc88bd038fe99c7f86fb132a15b60d0bf2f235b2c/"
               "hf_xet-1.7.0-cp38-abi3-win_amd64.whl"},
    "httpx2": {"version": "2.16.0", "module": "httpx2", "licence": "BSD-3-Clause", "size": 97880,
        "sha256": "fd2c5d7909e8d86fe92d53020c310ca0e4e431b838c207c5754b9bbf0308d49a",
        "url": _P + "7b/3d/2e4bc4fd1ef9770871669adb348d612a6e4786b303a23f9935b81c48dd60/"
               "httpx2-2.16.0-py3-none-any.whl"},
    "httpcore2": {"version": "2.16.0", "module": "httpcore2", "licence": "BSD-3-Clause", "size": 85070,
        "sha256": "5c4358319f2602d984ad93182c9fee5160f22e9da7babdda870a5e7c4e2dc34f",
        "url": _P + "af/c2/ce417c397afa5181f0e3c6ff01a4d01639414e1adef08730d97571297151/"
               "httpcore2-2.16.0-py3-none-any.whl"},
    "anyio": {"version": "4.15.1", "module": "anyio", "licence": "MIT", "size": 132079,
        "sha256": "6152fdbbf9a77fdec97731721bebf7c4c44f7c29b424b0065826173efc7ed101",
        "url": _P + "12/b8/4bd346e22b28902df4d651910f5242c28d84e4a5c2435ca5c3f797ed7e2e/"
               "anyio-4.15.1-py3-none-any.whl"},
    "h11": {"version": "0.16.0", "module": "h11", "licence": "MIT", "size": 37515,
        "sha256": "63cf8bbe7522de3bf65932fda1d9c2772064ffb3dae62d55932da54b31cb6c86",
        "url": _P + "04/4b/29cac41a4d98d144bf5f6d33995617b185d14b22401f75ca86f384e87ff1/"
               "h11-0.16.0-py3-none-any.whl"},
    "truststore": {"version": "0.10.5", "module": "truststore", "licence": "MIT", "size": 19017,
        "sha256": "9aaaedaefaf06d8b206278cf8b5012bc897f485a874503501e12d776df78951c",
        "url": _P + "51/e9/3a7820be2bb0fe53b6bc9c3be26d3d1158004e4c3ab953aa6840b955b1e9/"
               "truststore-0.10.5-py3-none-any.whl"},
    "idna": {"version": "3.20", "module": "idna", "licence": "BSD-3-Clause", "size": 69583,
        "sha256": "ab7ae7122974553370f0bdb919e1a960b2cd1bc1ef0276416d896db81c14582c",
        "url": _P + "58/a2/bb081bab032533a855d44de1d56f8e8426114ff1ba5d1f07a438a0a654f8/"
               "idna-3.20-py3-none-any.whl"},
    "tqdm": {"version": "4.70.1", "module": "tqdm", "licence": "MPL-2.0 AND MIT", "size": 80199,
        "sha256": "c293e525e6fef9c20e8728fd4612df02a0aa31bb5fe91ecd93e123b1b7bffa73",
        "url": _P + "a7/03/921a3d3c75785aca9ebfbfcabfbc3a1be12e2ab5265deb026d55a5a3f83e/"
               "tqdm-4.70.1-py3-none-any.whl"},
    "colorama": {"version": "0.4.6", "module": "colorama", "licence": "BSD-3-Clause", "size": 25335,
        "sha256": "4f1d9991f5acc0ca119f9d443620b77f9d6b33703e51011c16baf57afb285fc6",
        "url": _P + "d1/d6/3965ed04c63042e047cb6a3e6ed1a63a35087b6a609aa3a15ed8ac56c221/"
               "colorama-0.4.6-py2.py3-none-any.whl"},
    "filelock": {"version": "4.2.4", "module": "filelock", "licence": "MIT", "size": 162475,
        "sha256": "301241438c57936ef93f4243a94f2559022268bfcf9b375a9a72eb0f8db8358e",
        "url": _P + "e0/1b/5e74289a1878e666bb4b65fe5805a04cafcac2aa5f519ef1ead67ef91c56/"
               "filelock-4.2.4-py3-none-any.whl"},
    "fsspec": {"version": "2026.9.0", "module": "fsspec", "licence": "BSD-3-Clause", "size": 221738,
        "sha256": "8dd6e646e99ea382bd85f97a45e6b526a442d79423a7dc673f1e2756d05fcb5f",
        "url": _P + "6c/c0/a98505f18594f1bce828bb159cec0fcf9860562f1a2c85913409fc8f3d9e/"
               "fsspec-2026.9.0-py3-none-any.whl"},
    "packaging": {"version": "26.3", "module": "packaging", "licence": "Apache-2.0 OR BSD-2-Clause", "size": 129956,
        "sha256": "d7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c",
        "url": _P + "63/34/ba1c580383c9eada3711951fef0795c80b829a078d72188184bcab9dd527/"
               "packaging-26.3-py3-none-any.whl"},
    "pyyaml": {"version": "6.0.3", "module": "yaml", "licence": "MIT", "size": 154003,
        "sha256": "5fcd34e47f6e0b794d17de1b4ff496c00986e1c83f7ab2fb8fcfe9616ff7477b",
        "url": _P + "86/bf/899e81e4cce32febab4fb42bb97dcdf66bc135272882d1987881a4b519e9/"
               "pyyaml-6.0.3-cp312-cp312-win_amd64.whl"},
    "typing-extensions": {"version": "4.16.0", "module": "typing_extensions", "licence": "PSF-2.0", "size": 45571,
        "sha256": "481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8",
        "url": _P + "49/d3/b8441a820a491ddfc024b0b0cf0393375b75ea13866d9c66727e54c2fc80/"
               "typing_extensions-4.16.0-py3-none-any.whl"},
    "click": {"version": "8.5.0", "module": "click", "licence": "BSD-3-Clause", "size": 125251,
        "sha256": "255bc9599cf7748b4b1a446ccc735421bd08a2ae529a8b88597d3de5664ee360",
        "url": _P + "58/50/6c0d534c5f134586a8e1ba4e330569e32f057e33372ae556463212fb4cd3/"
               "click-8.5.0-py3-none-any.whl"},
    "numpy": {"version": "2.5.4", "module": "numpy", "licence": "BSD-3-Clause", "size": 12573091,
        "sha256": "a7b1b6353e36a7e50de2973a38d705c88ee93adcf120673cee7f45a4a3fa223a",
        "url": _P + "12/33/fefe62073dc8acfd0f2b9ed7c003af2f50aa61555e113e6db02b8f79f145/"
               "numpy-2.5.4-cp312-cp312-win_amd64.whl"},
    "sounddevice": {"version": "0.5.6", "module": "sounddevice", "licence": "MIT", "size": 1009630,
        "sha256": "7f4162f514f007b0bf25a3ccfed3f1705bc2ec311888a90232729eec4f57a4f4",
        "url": _P + "8d/3e/cbc593c31a5f0d817b3fe97e64aa8461bd0f55cb07b67ce1b776296ae336/"
               "sounddevice-0.5.6-py3-none-win_amd64.whl"},
    "cffi": {"version": "2.1.1", "module": "cffi", "licence": "MIT-0", "size": 185919,
        "sha256": "f53e442b08449d42821fa4a4fba000095af9f62742a500f978a9f557ec44339a",
        "url": _P + "d9/79/615cc094e2fb508cade7de88d3b4f6c4ec2bab695c97bce9153dc65aadf5/"
               "cffi-2.1.1-cp312-cp312-win_amd64.whl"},
    "pycparser": {"version": "3.12", "module": "pycparser", "licence": "BSD-3-Clause", "size": 62600,
        "sha256": "a076f3f489b39b43ed13563a1dafe42447445bec2e294154ec5c57fcdda4c85e",
        "url": _P + "b6/ae/315bbeee126a769cbde43cf4908acb4b40408d8bf8bbf334dd7368d8dadf/"
               "pycparser-3.12-py3-none-any.whl"},
    "webrtcvad-wheels": {"version": "2.0.14.post1", "module": "webrtcvad", "licence": "MIT", "size": 20671,
        "sha256": "a085ee7fa3f96ac7985ef0c1e3194e4c9c544cc9a0579b6dbd12c84d610271cc",
        "url": _P + "f9/1f/9f2bea3823af563197af5da572773510061f2e22c424257563c7e657882b/"
               "webrtcvad_wheels-2.0.14.post1-cp312-cp312-win_amd64.whl"},
    "nvidia-cublas-cu12": {"version": "12.9.2.10", "path": "nvidia/cublas", "licence": "NVIDIA proprietary", "size": 553162896,
        "sha256": "623f43027d40d44ceadf0043f002bd25cf353e8f13ce90b9a87057019f560661",
        "url": _P + "20/e2/fc9a0e985249d873150276d5afb02e39a66817fedbf1a385724393e505ed/"
               "nvidia_cublas_cu12-12.9.2.10-py3-none-win_amd64.whl"},
    "nvidia-cudnn-cu12": {"version": "9.27.0.42", "path": "nvidia/cudnn", "licence": "NVIDIA proprietary", "size": 743068852,
        "sha256": "06e9b0026f3bad97d2b58666330fabec04fe1672f776661ecb0ce0029c27f142",
        "url": _P + "aa/38/f856579877f7c1c5066e61182e7de7bc27bf35a78c8d1b0fa592e6985bc4/"
               "nvidia_cudnn_cu12-9.27.0.42-py3-none-win_amd64.whl"},
    "nvidia-cuda-nvrtc-cu12": {"version": "12.9.86", "path": "nvidia/cuda_nvrtc", "licence": "NVIDIA proprietary", "size": 76408187,
        "sha256": "72972ebdcf504d69462d3bcd67e7b81edd25d0fb85a2c46d3ea3517666636349",
        "url": _P + "52/de/823919be3b9d0ccbf1f784035423c5f18f4267fb0123558d58b813c6ec86/"
               "nvidia_cuda_nvrtc_cu12-12.9.86-py3-none-win_amd64.whl"},
    "onnxruntime-directml": {"version": "1.24.4", "module": "onnxruntime", "licence": "MIT", "size": 25111930,
        "sha256": "f2ecb68b7b7b259d2ef3112ae760149f9b5a1e7c0fbb73d539da6250a648a614",
        "url": _P + "88/ea/33814eb0ec96775eda4c1d30b0d86e91d7d2cd0d84c66d3915aef0e06fa3/"
               "onnxruntime_directml-1.24.4-cp312-cp312-win_amd64.whl"},
    # AirPlay casting (marquee.cast): pyatv and what it needs beyond what the app bundles
    "pyatv": {"version": "0.18.0", "module": "pyatv", "licence": "MIT", "size": 475244,
        "sha256": "3df6af5679eea809ff17954cf9f5d33c7e5ed5a8d1c90fa14090a0425ce5cd8a",
        "url": _P + "89/fa/5334e26d22cacd518a5d58836f48f1372126aba2711e93dca239906ed6ce/"
               "pyatv-0.18.0-py3-none-any.whl"},
    "aiohttp": {"version": "3.14.5", "module": "aiohttp", "licence": "Apache-2.0 AND MIT", "size": 506141,
        "sha256": "2cc38a4f2b516bef1714e690df87a0e043faf1a7693c82d860091684453d5111",
        "url": _P + "ae/ce/1f08114679d49655b30a6e0a29858375c94b82c1de1a0bd0a20c2fee8b02/"
               "aiohttp-3.14.5-cp312-cp312-win_amd64.whl"},
    "multidict": {"version": "7.2.1", "module": "multidict", "licence": "Apache-2.0", "size": 95372,
        "sha256": "781ca5dfc2f1d58e47f52216a0c1c7e1ba91ce8aa235459e20de2d402af77fb3",
        "url": _P + "68/d5/64108a33c21bde81454b5a400d16baa1378a7d01599de64b131c3e3e3114/"
               "multidict-7.2.1-cp312-cp312-win_amd64.whl"},
    "yarl": {"version": "1.25.1", "module": "yarl", "licence": "Apache-2.0", "size": 102593,
        "sha256": "7d575b54cb3863ef9bc290ea4b009999d55dc237326131e4853cf33e888fee03",
        "url": _P + "bb/cc/51a7b4abf4ac593b8e7eb3794b28e5a35ae26eed8bc04787628d215af82f/"
               "yarl-1.25.1-cp312-cp312-win_amd64.whl"},
    "aiohappyeyeballs": {"version": "2.7.1", "module": "aiohappyeyeballs", "licence": "PSF-2.0", "size": 15038,
        "sha256": "9243213661e29250eb41368e5daa826fc017156c3b8a11440826b2e3ed376472",
        "url": _P + "71/43/1947f06babed6b3f1d7f38b0c767f52df66bfb2bc10b468c4a7de9eceff2/"
               "aiohappyeyeballs-2.7.1-py3-none-any.whl"},
    "aiosignal": {"version": "1.4.0", "module": "aiosignal", "licence": "Apache-2.0", "size": 7490,
        "sha256": "053243f8b92b990551949e63930a839ff0cf0b0ebbe0597b0f3fb19e1a0fe82e",
        "url": _P + "fb/76/641ae371508676492379f16e2fa48f4e2c11741bd63c48be4b12a6b09cba/"
               "aiosignal-1.4.0-py3-none-any.whl"},
    "attrs": {"version": "26.1.0", "module": "attrs", "licence": "MIT", "size": 67548,
        "sha256": "c647aa4a12dfbad9333ca4e71fe62ddc36f4e63b2d260a37a8b83d2f043ac309",
        "url": _P + "64/b4/17d4b0b2a2dc85a6df63d1157e028ed19f90d4cd97c36717afef2bc2f395/"
               "attrs-26.1.0-py3-none-any.whl"},
    "chacha20poly1305-reuseable": {"version": "0.13.2", "module": "chacha20poly1305_reuseable", "licence": "Apache-2.0 OR BSD-3-Clause", "size": 8587,
        "sha256": "c7dba7c3604a9fa51bcfef9e4bfdaa3bfaadd88b6c5436a27be1c7e9c4081048",
        "url": _P + "fa/30/a4e44159996e832512f93ec6db89756ed72fe454ce3de1978e3a39c10bcd/"
               "chacha20poly1305_reuseable-0.13.2-py3-none-any.whl"},
    "cryptography": {"version": "50.0.2", "module": "cryptography", "licence": "Apache-2.0 OR BSD-3-Clause", "size": 3824692,
        "sha256": "7afa5a6602a9f29af1f3a2965f831bae7c9d5d597b7cbb716d41ab3b7d89879c",
        "url": _P + "9a/4f/adfc442765721292fff86d314ce385d3249d22db42295c0dd057727b60f3/"
               "cryptography-50.0.2-cp311-abi3-win_amd64.whl"},
    "frozenlist": {"version": "1.8.0", "module": "frozenlist", "licence": "Apache-2.0", "size": 44591,
        "sha256": "34187385b08f866104f0c0617404c8eb08165ab1272e884abc89c112e9c00746",
        "url": _P + "b8/af/38e51a553dd66eb064cdf193841f16f077585d4d28394c2fa6235cb41765/"
               "frozenlist-1.8.0-cp312-cp312-win_amd64.whl"},
    "miniaudio": {"version": "1.71", "module": "miniaudio", "licence": "MIT", "size": 274252,
        "sha256": "f4a44b70b66628b0c307e40ae0ae857695978cae18462179b806d8edc807d416",
        "url": _P + "fd/cf/c1a19e6800e725b6e2b4576407620a798d69e3528ebdea9aea84d69d7088/"
               "miniaudio-1.71-cp312-cp312-win_amd64.whl"},
    "propcache": {"version": "0.5.4", "module": "propcache", "licence": "Apache-2.0", "size": 46190,
        "sha256": "98914de2c4d7f0f9f4a8c6ea4bf05841f4175796941e3ef7d47eb718f22311fb",
        "url": _P + "58/7b/6e8ef26f6d510a7916064fec68d55fcbfbdf7eb01e377480d66a122152d8/"
               "propcache-0.5.4-cp312-cp312-win_amd64.whl"},
    "pydantic": {"version": "2.14.1", "module": "pydantic", "licence": "MIT", "size": 488106,
        "sha256": "9195d967ec791692a04438115466764fb8b9a27b31f14a760437694f40d6b454",
        "url": _P + "35/ea/a56b9fe5066f3537b7882f77e9c5dfb26d8c2eefdaed9b5fc73d57b4dc22/"
               "pydantic-2.14.1-py3-none-any.whl"},
    "pydantic-core": {"version": "2.50.1", "module": "pydantic_core", "licence": "MIT", "size": 2172522,
        "sha256": "132529c83901437ff642f585216831bf5fd7a91df66829907e155192ead62498",
        "url": _P + "c5/17/8bbd530b8659e9d963f5b16f6cb8e159f4cd174b4d6304e48969ab12dba8/"
               "pydantic_core-2.50.1-cp312-cp312-win_amd64.whl"},
    "annotated-types": {"version": "0.8.0", "module": "annotated_types", "licence": "MIT", "size": 13427,
        "sha256": "f072f4d804ea359e4eaf198b1af7a8b0943881a87f31bb764f8bf219bb9419e0",
        "url": _P + "99/91/8acff4f5e50511b911bbccb72b8628a49c68ce14148cd9f6431094859a90/"
               "annotated_types-0.8.0-py3-none-any.whl"},
    "srptools": {"version": "2.0.0", "module": "srptools", "licence": "BSD-3-Clause", "size": 13220,
        "sha256": "566b1cc55964996296b6389294c61f7ee8072f12de800bff4be625f79d930c9b",
        "url": _P + "72/90/a87a3f65f5d201e8732ad4ad9473ec24dc73c0e32764380b83604b8764e6/"
               "srptools-2.0.0-py3-none-any.whl"},
    "tabulate": {"version": "0.10.0", "module": "tabulate", "licence": "MIT", "size": 39814,
        "sha256": "f0b0622e567335c8fabaaa659f1b33bcb6ddfe2e496071b743aa113f8774f2d3",
        "url": _P + "99/55/db07de81b5c630da5cbf5c7df646580ca26dfaefa593667fc6f2fe016d2e/"
               "tabulate-0.10.0-py3-none-any.whl"},
    "tinytag": {"version": "2.3.2", "module": "tinytag", "licence": "MIT", "size": 37171,
        "sha256": "ebaf957915266b9c20414a10f8c5170a345aa039a794650d39d66e7d6b96199f",
        "url": _P + "6b/54/d378858d14e5c5c7b921cb9ffccf27be3d34f3d6d010c57404ddd634685d/"
               "tinytag-2.3.2-py3-none-any.whl"},
    "typing-inspection": {"version": "0.4.4", "module": "typing_inspection", "licence": "MIT", "size": 14750,
        "sha256": "65b8397ba37ccbce054456aaccddfc91e6e3083c92824df348d96ca832f3f147",
        "url": _P + "67/81/4add07e5172b7ac40d8ed5ff580409a7801a4fe26d529bdd915401dabfbe/"
               "typing_inspection-0.4.4-py3-none-any.whl"},
}

GROUPS = {
    # scene search / sensitive ranges (index/deps.py)
    "index": ["numpy", "onnxruntime-directml", "tokenizers"],
    # speech to text and microphones (assistant). Not onnxruntime: faster-whisper only imports it for its Silero
    # VAD (vad_filter=True), which Atlas doesn't use (webrtcvad does that job); when the index is installed its
    # onnxruntime-directml provides the same module, so the two never overwrite each other's onnxruntime folder.
    "voice": ["numpy", "sounddevice", "cffi", "pycparser", "webrtcvad-wheels",
              "faster-whisper", "ctranslate2", "av", "tokenizers", "pyyaml", "tqdm", "colorama",
              "huggingface-hub", "hf-xet", "httpx2", "httpcore2", "anyio", "h11", "idna", "truststore",
              "click", "filelock", "fsspec", "packaging", "typing-extensions"],
    # CUDA 12 / cuDNN 9 runtime for faster-whisper on NVIDIA GPUs (large)
    "gpu": ["nvidia-cublas-cu12", "nvidia-cudnn-cu12", "nvidia-cuda-nvrtc-cu12"],
    # AirPlay casting (marquee.cast.blueprint.AIRPLAY_WHEELS): pyatv, fetched the first time an AirPlay TV is picked
    "airplay": ["pyatv", "aiohttp", "multidict", "yarl", "propcache", "frozenlist", "aiosignal", "aiohappyeyeballs",
                "attrs", "idna", "cryptography", "cffi", "pycparser", "chacha20poly1305-reuseable", "miniaudio",
                "pydantic", "pydantic-core", "annotated-types", "typing-inspection", "typing-extensions", "srptools",
                "tabulate", "tinytag"],
}

_lock = threading.Lock()


def label(name):
    w = WHEELS[name]
    return f"{name} {w['version']} ({w['licence']})"


def add_lib_path():
    """Make unpacked wheels importable (after the environment's own packages)."""
    p = str(LIB_DIR)
    if LIB_DIR.is_dir() and p not in sys.path:
        sys.path.append(p)
        importlib.invalidate_caches()


def present(name, deep=False):
    """Is this wheel's content available? deep: actually import the module (catches broken installs)."""
    add_lib_path()
    w = WHEELS[name]
    if "path" in w:
        return any((Path(p) / w["path"] / "bin").is_dir() for p in sys.path if p)
    try:
        if deep:
            importlib.import_module(w["module"])
            return True
        return importlib.util.find_spec(w["module"]) is not None
    except Exception:
        return False


def missing(names, deep=False):
    """The wheels install() would fetch: [{kind, name, size}]."""
    return [{"kind": "package", "name": label(n), "size": WHEELS[n]["size"]} for n in names if not present(n, deep)]


def download(url, dest: Path, sha256, progress=None):
    """Stream url to dest, checking its SHA-256; progress["done"] grows by the bytes read."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    try:
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(part, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
                    h.update(chunk)
                    if progress is not None:
                        progress["done"] = progress.get("done", 0) + len(chunk)
        if sha256 and h.hexdigest() != sha256:
            raise IOError(f"{dest.name}: checksum mismatch (got {h.hexdigest()})")
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)


def _move_tree(src: Path, dst: Path):
    """Move src into dst, merging folders (several nvidia-* wheels share the nvidia\\ folder)."""
    if src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        for c in src.iterdir():
            _move_tree(c, dst / c.name)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)


def _unpack(whl: Path, name):
    """Unpack into a staging folder first, then move into LIB_DIR with the .dist-info last, so a half-done unpack
    never looks installed."""
    stage = LIB_DIR.parent / f".unpack-{name}"
    shutil.rmtree(stage, ignore_errors=True)
    with zipfile.ZipFile(whl) as z:
        z.extractall(stage)
    for data in stage.glob("*.data"):
        for sub in ("purelib", "platlib"):
            if (data / sub).is_dir():
                _move_tree(data / sub, stage)
        shutil.rmtree(data, ignore_errors=True)
    infos = list(stage.glob("*.dist-info"))
    LIB_DIR.mkdir(parents=True, exist_ok=True)
    for entry in stage.iterdir():
        if entry not in infos:
            _move_tree(entry, LIB_DIR / entry.name)
    for info in infos:   # drop an older version's metadata, then add this one
        for old in LIB_DIR.glob(info.name.split("-")[0] + "-*.dist-info"):
            shutil.rmtree(old, ignore_errors=True)
        _move_tree(info, LIB_DIR / info.name)
    shutil.rmtree(stage, ignore_errors=True)


def install(names, progress=None, deep=False):
    """Fetch and unpack the given wheels that are missing. Blocking. progress (a dict) gets step/done/total."""
    progress = progress if progress is not None else {}
    todo = [n for n in names if not present(n, deep)]
    if not progress.get("total"):
        progress["total"] = progress.get("done", 0) + sum(WHEELS[n]["size"] for n in todo)
    for n in todo:
        with _lock:   # the index and the assistant may install at once; they share some wheels
            if present(n, deep):
                progress["done"] = progress.get("done", 0) + WHEELS[n]["size"]
                continue
            w = WHEELS[n]
            progress["step"] = label(n)
            whl = DOWNLOADS / w["url"].rsplit("/", 1)[1]
            download(w["url"], whl, w["sha256"], progress)
            try:
                _unpack(whl, n)
            finally:
                whl.unlink(missing_ok=True)
            add_lib_path()
            importlib.invalidate_caches()
    return todo
