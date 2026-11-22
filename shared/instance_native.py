"""Pinned Codex native package layout and fail-closed local verification."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile

from shared import instance_config as cfg

PREVIOUS_VERSION = '0.160.0'
VERSION = '0.171.0'
_PREVIOUS_URL = 'https://github.com/openai/codex/releases/download/rust-v0.160.0/codex-package-x86_64-unknown-linux-musl.tar.gz'
_PREVIOUS_ARCHIVE_SIZE = 160727443
_PREVIOUS_ARCHIVE_SHA256 = '4fcc47ab57f52ff75363951a8761146cd10c8288bd86fed45487dbb204a16b71'
_PREVIOUS_MEMBERS = (('bin', '5', 0, None),
 ('bin/codex', '0', 289101384, '12eb3e81114588aca3b7998f4f19e8997b056aca08e57a7ca7c8a3ec8c652aad'),
 ('bin/codex-code-mode-host', '0', 74072976, '37cab1584302611e9936902219640ab5e7a79fcfccd2504c6e85ea8cb97d0e10'),
 ('codex-package.json', '0', 205, '2aa6a23f733c278a9bf385f0725b97b76708559d31209636cb165c1c872a40ef'),
 ('codex-path', '5', 0, None),
 ('codex-path/rg', '0', 5408904, 'e62198eb19b136b88c330af83647b5a962cb99b6b1f066758568f12de1974849'),
 ('codex-resources', '5', 0, None),
 ('codex-resources/bwrap', '0', 529776, '01fb705f067bd5365b63d8ad2323a61c8d007733ca5e649437e086f3fb9935d8'),
 ('codex-resources/voice', '5', 0, None),
 ('codex-resources/voice/NOTICE.md', '0', 2403, '40097a1799441051aa0b6645cdc819cceb19be73cdd265eb602aa2b15e0b1f48'),
 ('codex-resources/voice/bin', '5', 0, None),
 ('codex-resources/voice/bin/codex-voice-host',
  '0',
  64531056,
  'b5b871df347f59b4cf5f82f438caa054ccfaa6a0d5c00cddbfb830655ab4ed9f'),
 ('codex-resources/voice/lib', '5', 0, None),
 ('codex-resources/voice/lib/gstreamer-1.0', '5', 0, None),
 ('codex-resources/voice/lib/gstreamer-1.0/libgstapp.so',
  '0',
  6488,
  'd9e4f502e5c2aeb2e9470d5efaa5a8657cb9bacddce106bc88facea92becb366'),
 ('codex-resources/voice/lib/gstreamer-1.0/libgstaudioconvert.so',
  '0',
  40928,
  '683202e93824c36fa7acecd9de6df2ad891110ebbc959baebe02ed4e873d3c50'),
 ('codex-resources/voice/lib/gstreamer-1.0/libgstaudioresample.so',
  '0',
  35120,
  'fffe83239de05df3f6acaf9203231b1b37ab39fe17aaf4cf8516de99aa5ca1ba'),
 ('codex-resources/voice/lib/gstreamer-1.0/libgstcoreelements.so',
  '0',
  473176,
  '44356d5e6867acdd76966aa5395428f63b4954389e85e0f748564d5989422aa1'),
 ('codex-resources/voice/lib/gstreamer-1.0/libgstopus.so',
  '0',
  71992,
  '39874376534b8d5f72fe6bbe62f4df258e33dba76a2f66bf5f3389940aa4f752'),
 ('codex-resources/voice/lib/gstreamer-1.0/libgstrtp.so',
  '0',
  739744,
  '7a4599d312c3a00a1794f9b99481ac147cee7554b6d36d1479df6b7fd019b2a8'),
 ('codex-resources/voice/lib/gstreamer-1.0/libgstrtpmanager.so',
  '0',
  558104,
  'c1f55dad0daedc45b189e970a46519bb065420f704c2265d95e040686b6adebb'),
 ('codex-resources/voice/lib/libffi.so.8',
  '0',
  63112,
  '6ef5db59b2e4b1eb63032fa6c8afb1669bc09b073789233c379efda85b1353db'),
 ('codex-resources/voice/lib/libgio-2.0.so.0',
  '0',
  2307904,
  '3b3cc57ae7b680ba5efd0ff9fdcabb9fe324a76d2242762ba6ac3b6240e1a7ab'),
 ('codex-resources/voice/lib/libglib-2.0.so.0',
  '0',
  1480224,
  'edfa7989861b231b46b564dbad9e0445bd6788e088f1ab60f0a85c4a61428a44'),
 ('codex-resources/voice/lib/libgmodule-2.0.so.0',
  '0',
  15656,
  'e8e87e59f24569ad3c20b039eefce03aea811eee10352904e33b726e53f8d22f'),
 ('codex-resources/voice/lib/libgobject-2.0.so.0',
  '0',
  449600,
  '5e3fcf7f22575d0c3295589ae44d07b3bef234aa18ee8aea45317a7380783b16'),
 ('codex-resources/voice/lib/libgstallocators-1.0.so.0',
  '0',
  30128,
  '43873f69fa9e165c375ad9bfe3b13a12610c34765a4fed2a734c1a174e9ffa74'),
 ('codex-resources/voice/lib/libgstapp-1.0.so.0',
  '0',
  105200,
  '0b06343f03ac90e03345ceee1a69121676bead76b87df9c2c3ebadb3acc5cdc2'),
 ('codex-resources/voice/lib/libgstaudio-1.0.so.0',
  '0',
  597832,
  '864c5ee92de639bc699c354b548eab085aa222e87e0051918db31c86b741d5d4'),
 ('codex-resources/voice/lib/libgstbase-1.0.so.0',
  '0',
  564592,
  'e215a5fcd03f52f4492a898a582217d8033c32e730f9221989c9e19032b99a6f'),
 ('codex-resources/voice/lib/libgstnet-1.0.so.0',
  '0',
  73856,
  '9d19251c9b72b9c9049cc157f9a921fdc527111b0a4d349d87385a7bd3d2663f'),
 ('codex-resources/voice/lib/libgstpbutils-1.0.so.0',
  '0',
  316576,
  'b07f4b00770e3f9a5700803374995717ad76b7f045bd15b4d401f3b83d2d379f'),
 ('codex-resources/voice/lib/libgstreamer-1.0.so.0',
  '0',
  1548568,
  'ebcbaaaad2bf51f483cd543aa3579cb77b8a128b835152a1481429324fbe98be'),
 ('codex-resources/voice/lib/libgstrtp-1.0.so.0',
  '0',
  209200,
  'bb5d6207c907f0906fbc8a48262149be2953041f0124b556aa3ec2b511d4b97e'),
 ('codex-resources/voice/lib/libgsttag-1.0.so.0',
  '0',
  268280,
  '7b0931a2f006733cb7a2b5729b34dc5257b7f3e46cd8a2770bb0d01086af56c7'),
 ('codex-resources/voice/lib/libgstvideo-1.0.so.0',
  '0',
  975136,
  '7f10b05f6716115b5b2a7eb284dfc8136d653d940ba253d10d269fcfd7527cd7'),
 ('codex-resources/voice/lib/libintl.so.8',
  '0',
  6288,
  'f50f3ab34be7b3392f2db82bd67d50e133b35fa41d352dc3b3a97b70b0ffd960'),
 ('codex-resources/voice/lib/libopus.so.0',
  '0',
  565744,
  'a14170fac3704ed2ef79e030c6fc85ea7058a2d6e9f3cfacd8f47a6f63deb976'),
 ('codex-resources/voice/lib/libpcre2-8.so.0',
  '0',
  442424,
  'e39519b242f40686734ed8d0218f68cac24f0e956ec64c8c1b49b9da5cc9788b'),
 ('codex-resources/voice/lib/libz.so.1',
  '0',
  109296,
  '6c5d765f42d9368cc4bdbd11f725c1bcf6b14b78322033d3c94f787fc6261f5e'),
 ('codex-resources/voice/licenses', '5', 0, None),
 ('codex-resources/voice/licenses/LGPL-2.1.txt',
  '0',
  26431,
  'ad2eec519ebd4b5df86ea84dff24ae3bfa2edea846a703b58902dd221ae375db'),
 ('codex-resources/voice/licenses/Opus.txt',
  '0',
  1945,
  '01e1167d54a096d123cf6dfbbeb19587278845c6481d2d66d545669846079551'),
 ('codex-resources/voice/licenses/PCRE2.md',
  '0',
  4011,
  '197d8a73ffee0d6b09adba2f9c677b5f5aede24edf89258a68e48248d010d811'),
 ('codex-resources/voice/licenses/libffi.txt',
  '0',
  1132,
  '17b64dc60f3b6897a60f971e288b973f655c2edcdf08b25f3c3dd5549857881c'),
 ('codex-resources/voice/licenses/proxy-libintl.txt',
  '0',
  25292,
  'd245807f90032872d1438d741ed21e2490e1175dc8aa3afa5ddb6c8e529b58e5'),
 ('codex-resources/voice/licenses/sljit.txt',
  '0',
  1444,
  '5f216505c0f6ea3273caec89e766eef93cdeb7bbb0c429f9360116d7c938feeb'),
 ('codex-resources/voice/licenses/zlib.txt',
  '0',
  1002,
  'e32ff4e00d9d94930537635291da39e7e612703334bf6fde8c7f1686fe8a45a2'),
 ('codex-resources/voice/manifest.json', '0', 4768, 'c7c90399f39b195d71685d6fe9d9b852bda9c1ede5305b19c79d8942f54b107d'),
 ('codex-resources/voice/runtime.json', '0', 13941, 'a64f6acd58efb50f606c43825393e3d28bdd6604e89abe0523c4e9a618795083'),
 ('codex-resources/voice/sources.json', '0', 4911, 'e2f6b124f6277e7c3c4f5c7cc8830c66190f671ad65e972bb2cc9507dbfcb937'),
 ('codex-resources/zsh', '5', 0, None),
 ('codex-resources/zsh/bin', '5', 0, None),
 ('codex-resources/zsh/bin/zsh', '0', 898480, '67faaaa89242c4a332e16e508a1977cffc24bf7fca31d4411cdfd101f3831ef3'))
LICENSE_NAME = "LICENSE.txt"
LICENSE_SIZE = 10926
LICENSE_SHA256 = "d17f227e4df5da1600391338865ce0f3055211760a36688f816941d58232d8dc"
_PREVIOUS_EXECUTABLES = frozenset({
    "bin/codex", "bin/codex-code-mode-host", "codex-path/rg",
    "codex-resources/bwrap", "codex-resources/voice/bin/codex-voice-host",
    "codex-resources/zsh/bin/zsh",
})


# Verified upstream full Linux x64 kit; the source ZIP does not redistribute it.
URL = 'https://github.com/openai/codex/releases/download/rust-v0.171.0/codex-package-x86_64-unknown-linux-musl.tar.gz'
ARCHIVE_SIZE = 197763180
ARCHIVE_SHA256 = '8391d67757496bfd14893eca6cbf0c96994bf2627566ff1f018f70600c9ef6b3'
MEMBERS = (('bin', '5', 0, None),
 ('bin/codex', '0', 315109048, 'b76a027d6b210f9a8da7aaf321dd235c700f0ef7a794d4a5314b9af6c134f0f0'),
 ('bin/codex-code-mode-host', '0', 70072856, 'f49bd4f0c0cc73cf1ec3a719009e07ce5b6e100520d023c760cc203fb37e5c6f'),
 ('bin/codex-exec-server', '0', 77627248, 'da2b3636f0b050e4b97da5cf8ed6b3f85621d7611105f58ee9748d8c7e2266fe'),
 ('codex-package.json', '0', 246, 'dd7e534b6f3474b0bb01934d05dfd9f0dfbf848502f0465c8116bb04d616c20b'),
 ('codex-path', '5', 0, None),
 ('codex-path/rg', '0', 5408904, 'e62198eb19b136b88c330af83647b5a962cb99b6b1f066758568f12de1974849'),
 ('codex-resources', '5', 0, None),
 ('codex-resources/bwrap', '0', 492984, '91a245ea678a7ae642f383a22576c3c476d0d6d3e2321a553f3ed8d4d487ed74'),
 ('codex-resources/voice', '5', 0, None),
 ('codex-resources/voice/NOTICE.md', '0', 2403, '40097a1799441051aa0b6645cdc819cceb19be73cdd265eb602aa2b15e0b1f48'),
 ('codex-resources/voice/bin', '5', 0, None),
 ('codex-resources/voice/bin/codex-voice-host',
  '0',
  40481464,
  '1b072d76224a57aaaca75497bcb2b5c458f6f21467c6c09cf137a4aa529e4ff3'),
 ('codex-resources/voice/lib', '5', 0, None),
 ('codex-resources/voice/lib/gstreamer-1.0', '5', 0, None),
 ('codex-resources/voice/lib/gstreamer-1.0/libgstapp.so',
  '0',
  6488,
  '140af8f4c349124d1c95fb28752c1320b1c1cd8830b4e44ecaf9aec0eb868631'),
 ('codex-resources/voice/lib/gstreamer-1.0/libgstaudioconvert.so',
  '0',
  41040,
  '9d85c49871bd4aced64ab7395552e4deabc2bb675885b1405f3cc5d93f36de28'),
 ('codex-resources/voice/lib/gstreamer-1.0/libgstaudioresample.so',
  '0',
  35248,
  '17caea8022342bb088dcf6b9a3369eb1d88ee94abc57d600de65b797e704cc86'),
 ('codex-resources/voice/lib/gstreamer-1.0/libgstcoreelements.so',
  '0',
  479464,
  '562f60c464252841d10807675fe800c4a0c673910771562ae57e1b653058a22a'),
 ('codex-resources/voice/lib/gstreamer-1.0/libgstopus.so',
  '0',
  73496,
  'a5f9e9fb8700e53f727414085b47ba7dc4124855df047238f1bc52a95033fb91'),
 ('codex-resources/voice/lib/gstreamer-1.0/libgstrtp.so',
  '0',
  759304,
  'ad625aae7c96e2ac07c124fe86d04e3e47d574723f94c84b4334abbcb7ade2c8'),
 ('codex-resources/voice/lib/gstreamer-1.0/libgstrtpmanager.so',
  '0',
  563976,
  'b8199fdd0f38f40cd4f7b075792d73824914c14f6fc9886829bddf3abce41bd5'),
 ('codex-resources/voice/lib/libffi.so.8', '0', 63088, '9343712fef5adb7f0d00b6f9c96d3b76c7eaed0ff3f96b1db665c221e68c2c7c'),
 ('codex-resources/voice/lib/libgio-2.0.so.0',
  '0',
  2336176,
  'b8e821b78ebf2bf6f44b7a01ce94b029f3bba6c5469b399a1406ac83eaf17bcf'),
 ('codex-resources/voice/lib/libglib-2.0.so.0',
  '0',
  1520592,
  'c352bfa2e91f7b55fa5811d2c13a2e302f75011720d7cccc24ecea386ec2d115'),
 ('codex-resources/voice/lib/libgmodule-2.0.so.0',
  '0',
  15736,
  '7ab0e8a6e611e90f49f3c05d40fd0414468088936c684f186ab28448d4fcdc64'),
 ('codex-resources/voice/lib/libgobject-2.0.so.0',
  '0',
  455408,
  '6e9f18a820d323996059ec5bc61022ca4f2616e2f1aceb62873f492bea02d080'),
 ('codex-resources/voice/lib/libgstallocators-1.0.so.0',
  '0',
  30352,
  'fafd125d171e5e1380ab4f7516d5271910894a1dd25f257eb0d6e5d9b0a3a453'),
 ('codex-resources/voice/lib/libgstapp-1.0.so.0',
  '0',
  106128,
  'f8c71b4888187b963ae3afcf403f5cf54f05e566f14fec9a3674f2723b69ce21'),
 ('codex-resources/voice/lib/libgstaudio-1.0.so.0',
  '0',
  637664,
  '45087b6be7d7c315d9a0f554c0e003684df6cf8823e9187f85047d2f50310ecc'),
 ('codex-resources/voice/lib/libgstbase-1.0.so.0',
  '0',
  573472,
  'c858b1b65ba92e1104b026d119a59fdd817e73651f2f7f8e60b915dcb84a823d'),
 ('codex-resources/voice/lib/libgstnet-1.0.so.0',
  '0',
  74160,
  '2984c7c5e7ced0da7bebc79f734a02f0cd9009daff52ef60c3b5cc51c7004435'),
 ('codex-resources/voice/lib/libgstpbutils-1.0.so.0',
  '0',
  323208,
  '601e271b6c2be4ee5f2cf298d74356563b5c4ecf76ddb2c53945b1801feff896'),
 ('codex-resources/voice/lib/libgstreamer-1.0.so.0',
  '0',
  1582984,
  '4114014dd07e0cf59b80eb50db8fcf9e2e2a44bde53cdbd930ccaf36038440d3'),
 ('codex-resources/voice/lib/libgstrtp-1.0.so.0',
  '0',
  211696,
  '984a3430ecffee72d202030df13b8c15cfc84ce1644e9ca53248ef8c10e25d9e'),
 ('codex-resources/voice/lib/libgsttag-1.0.so.0',
  '0',
  273992,
  '3fc269705e4ee71bb11f6c259f8a2a739e9bafbb5fb90d7148a03bf1ac7f6abd'),
 ('codex-resources/voice/lib/libgstvideo-1.0.so.0',
  '0',
  1006016,
  '307f9bb3017236f60c6bbbaa7a26e5e8889298619e7c7917deef4362e939d6ce'),
 ('codex-resources/voice/lib/libintl.so.8', '0', 6304, '52508cf6dadff7cc431dd76d1f36754eb4184f56a49f93442f1a48bcb240df20'),
 ('codex-resources/voice/lib/libopus.so.0', '0', 563184, '50d6c07e5863eecea08fd2e7e9a851b8d46b0b74f8b57645eac2ee54d5087a6d'),
 ('codex-resources/voice/lib/libpcre2-8.so.0',
  '0',
  441864,
  '6d589f935f093df4fbcebf01b7f8afa997378f4a4ac5fd0a76e0c5f604af64b7'),
 ('codex-resources/voice/lib/libz.so.1', '0', 110112, '7088b2cc0f2812f6617666d851bd779925de4350a87ef0a288bbedda87e76083'),
 ('codex-resources/voice/licenses', '5', 0, None),
 ('codex-resources/voice/licenses/LGPL-2.1.txt',
  '0',
  26431,
  'ad2eec519ebd4b5df86ea84dff24ae3bfa2edea846a703b58902dd221ae375db'),
 ('codex-resources/voice/licenses/Opus.txt', '0', 1945, '01e1167d54a096d123cf6dfbbeb19587278845c6481d2d66d545669846079551'),
 ('codex-resources/voice/licenses/PCRE2.md', '0', 4011, '197d8a73ffee0d6b09adba2f9c677b5f5aede24edf89258a68e48248d010d811'),
 ('codex-resources/voice/licenses/libffi.txt', '0', 1132, '17b64dc60f3b6897a60f971e288b973f655c2edcdf08b25f3c3dd5549857881c'),
 ('codex-resources/voice/licenses/proxy-libintl.txt',
  '0',
  25292,
  'd245807f90032872d1438d741ed21e2490e1175dc8aa3afa5ddb6c8e529b58e5'),
 ('codex-resources/voice/licenses/sljit.txt', '0', 1444, '5f216505c0f6ea3273caec89e766eef93cdeb7bbb0c429f9360116d7c938feeb'),
 ('codex-resources/voice/licenses/zlib.txt', '0', 1002, 'e32ff4e00d9d94930537635291da39e7e612703334bf6fde8c7f1686fe8a45a2'),
 ('codex-resources/voice/manifest.json', '0', 4768, '2488496113f14d1ac5705f6b0cbc1d4ec88f9234c1820d3b1380050363676ef3'),
 ('codex-resources/voice/runtime.json', '0', 13941, 'fbadfc83a42b63ac132f12c909ed8072d6056b5648c8f88c57937c3763648c13'),
 ('codex-resources/voice/sources.json', '0', 4911, '5613f37012effc3e28cdad52a7c88b9098aaaf7aae43f3cb7199f55ffbb15908'))
EXECUTABLES = _PREVIOUS_EXECUTABLES | {'bin/codex-exec-server'}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for part in iter(lambda: source.read(1024 * 1024), b""):
            value.update(part)
    return value.hexdigest()


def _pin(version: str | None = None):
    if version is None or version == VERSION:
        return URL, ARCHIVE_SIZE, ARCHIVE_SHA256, MEMBERS, EXECUTABLES
    if version == PREVIOUS_VERSION:
        return _PREVIOUS_URL, _PREVIOUS_ARCHIVE_SIZE, _PREVIOUS_ARCHIVE_SHA256, _PREVIOUS_MEMBERS, _PREVIOUS_EXECUTABLES
    _error()


def manifest_identity(version: str | None = None) -> str:
    members = _pin(version)[3]
    return hashlib.sha256(json.dumps({"members": members, "license": (LICENSE_SIZE, LICENSE_SHA256)}, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def metadata(version: str | None = None) -> dict:
    version = VERSION if version is None else version
    url, size, checksum, _, _ = _pin(version)
    return {"format_version": 2, "version": version, "source": url,
            "archive_size": size, "archive_sha256": checksum, "manifest_sha256": manifest_identity(version)}


def known_metadata(value: object) -> bool:
    return value == metadata() or value == metadata(PREVIOUS_VERSION)


def _metadata_version(value: dict | None) -> str:
    if value is None:
        return VERSION
    if not known_metadata(value):
        _error()
    return value["version"]


def command(root: Path) -> Path:
    return root / "native" / "bin" / "codex"


def _error(code="native_digest_mismatch") -> None:
    raise cfg.InstanceError("原生完整安装包不符合已核验的官方固定版本，未执行。", code)


def _relative(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or str(path) != name:
        _error("unsafe_native_archive")
    return path


def _expected(version: str) -> dict[str, tuple[str, int, str | None]]:
    return {name: (kind, size, checksum) for name, kind, size, checksum in _pin(version)[3]}


def _private_dir(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        _error("unsafe_permissions")


def _verify_tree(root: Path, version: str) -> None:
    expected = _expected(version)
    executables = _pin(version)[4]
    try:
        actual = {relative: (path, info) for relative, path, info in _entries(root)}
    except (OSError, ValueError):
        _error()
    if set(actual) != set(expected) | {LICENSE_NAME}:
        _error()
    for name, (kind, size, checksum) in expected.items():
        path, info = actual[name]
        if kind == "5":
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                _error()
        else:
            mode = 0o700 if name in executables else 0o600
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != mode or info.st_size != size or digest(path) != checksum):
                _error()
    license_path, license_info = actual[LICENSE_NAME]
    if (not stat.S_ISREG(license_info.st_mode) or license_info.st_uid != os.getuid()
            or license_info.st_nlink != 1 or stat.S_IMODE(license_info.st_mode) != 0o600
            or license_info.st_size != LICENSE_SIZE or digest(license_path) != LICENSE_SHA256):
        _error()


def _entries(root: Path):
    _private_dir(root)
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        base = Path(current)
        # os.walk lists symlink directories in dirs; lstat all entries before use.
        for name in sorted(dirs + files):
            path = base / name
            info = path.lstat()
            relative = path.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                _error()
            yield relative, path, info


def verify(root: Path, value: dict | None = None) -> None:
    _verify_tree(root, _metadata_version(value))


def unpack(archive: Path, destination: Path, value: dict | None = None) -> None:
    """Extract only the pinned files into an empty private staging directory."""
    version = _metadata_version(value)
    expected = _expected(version)
    executables = _pin(version)[4]
    seen: set[str] = set()
    try:
        with tarfile.open(archive, "r:gz") as data:
            for item in data:
                name = item.name.rstrip("/")
                _relative(name)
                if name in seen or name not in expected:
                    _error("unsafe_native_archive")
                seen.add(name)
                kind, size, checksum = expected[name]
                if ((kind == "5" and not item.isdir()) or (kind == "0" and not item.isreg())
                        or item.size != size or item.issym() or item.islnk()):
                    _error("unsafe_native_archive")
                target = destination.joinpath(*PurePosixPath(name).parts)
                if kind == "5":
                    target.mkdir(mode=0o700)
                    target.chmod(0o700)
                    continue
                stream = data.extractfile(item)
                if stream is None:
                    _error("unsafe_native_archive")
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with stream, os.fdopen(fd, "wb") as output:
                    shutil.copyfileobj(stream, output)
                    output.flush(); os.fsync(output.fileno())
                target.chmod(0o700 if name in executables else 0o600)
                if digest(target) != checksum:
                    _error()
    except (tarfile.TarError, OSError):
        _error("unsafe_native_archive")
    if seen != set(expected):
        _error("unsafe_native_archive")


def add_license(destination: Path, source: Path) -> None:
    target = destination / LICENSE_NAME
    shutil.copyfile(source, target)
    target.chmod(0o600)


def copy_bundle(source: Path, destination: Path, value: dict | None = None) -> None:
    verify(source, value)
    _, _, _, members, executables = _pin(_metadata_version(value))
    destination.mkdir(mode=0o700)
    for name, kind, _, _ in members:
        target = destination / name
        original = source / name
        if kind == "5":
            target.mkdir(mode=0o700)
            target.chmod(0o700)
        else:
            shutil.copyfile(original, target)
            target.chmod(0o700 if name in executables else 0o600)
    shutil.copyfile(source / LICENSE_NAME, destination / LICENSE_NAME)
    (destination / LICENSE_NAME).chmod(0o600)
    verify(destination, value)
