"""The reader site: bundle contents and encryption that the browser can actually decrypt."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from whats_cooking.config import ROOT
from whats_cooking.site import build_bundle, build_site, decrypt, encrypt

# Same Web Crypto calls as site/app.js (deriveKey + decryptWith).
NODE_DECRYPT = r"""
const blob = JSON.parse(require("fs").readFileSync(process.argv[2], "utf8"));
const b64 = (s) => Uint8Array.from(Buffer.from(s, "base64"));
(async () => {
  const base = await crypto.subtle.importKey("raw", new TextEncoder().encode(process.argv[3]), "PBKDF2", false, ["deriveKey"]);
  const key = await crypto.subtle.deriveKey({ name: "PBKDF2", hash: "SHA-256", salt: b64(blob.salt), iterations: blob.iter },
    base, { name: "AES-GCM", length: 256 }, true, ["decrypt"]);
  const plain = await crypto.subtle.decrypt({ name: "AES-GCM", iv: b64(blob.iv) }, key, b64(blob.ct));
  process.stdout.write(new TextDecoder().decode(plain));
})().catch((e) => { console.error(String(e)); process.exit(1); });
"""


def test_encrypt_roundtrip_and_wrong_passphrase():
    blob = encrypt(b'{"hello": "paper"}', "correct horse")
    assert decrypt(blob, "correct horse") == b'{"hello": "paper"}'
    with pytest.raises(Exception):
        decrypt(blob, "wrong")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_browser_crypto_can_decrypt_python_output(tmp_path):
    payload = json.dumps({"editions": [{"day": "2026-10-16", "headline": "Café ünïcode"}]}, ensure_ascii=False).encode()
    f = tmp_path / "blob.json"
    f.write_text(json.dumps(encrypt(payload, "a long passphrase")))
    script = tmp_path / "dec.js"
    script.write_text(NODE_DECRYPT)
    out = subprocess.run(["node", str(script), str(f), "a long passphrase"], capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["editions"][0]["headline"] == "Café ünïcode"


def test_build_site_encrypted_has_no_plain_data(db, settings, tmp_path):
    settings.root = ROOT                       # use the real site/ sources
    out = tmp_path / "site"
    info = build_site(settings, db, out, "pass phrase")
    assert info["encrypted"] and (out / "data.enc.json").exists() and not (out / "data.json").exists()
    index = (out / "index.html").read_text(encoding="utf-8")
    assert "noindex" in index and "app.js" in index
    bundle = json.loads(decrypt(json.loads((out / "data.enc.json").read_text()), "pass phrase"))
    assert set(bundle) == {"generated_at", "editions", "problems", "long_game", "wire", "communities", "index",
                           "topics_scored", "collection", "request_url"}


def test_bundle_shapes_on_empty_db(db):
    b = build_bundle(db)
    assert b["editions"] == [] and b["wire"] == []
    assert set(b["problems"]) == {"analysed", "requests", "emerging"}
