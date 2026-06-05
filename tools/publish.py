#!/usr/bin/env python3
"""
publish.py — push a OneStop review page to the onestop-share GitHub Pages hub.

Takes a carousel / deck / website HTML file, flattens it to ONE self-contained
page (only the images it actually references, web-compressed and inlined as data
URIs), writes it to <slug>/index.html, regenerates the hub index, and (with
--push) commits + pushes. The team opens one link; re-publishing keeps the link
the same and just updates the content.

Usage:
  python3 tools/publish.py --title "MF Fees" --in <path.html> [--slug mffees] [--blurb "..."] [--push]
  python3 tools/publish.py --list
  python3 tools/publish.py --remove <slug> [--push]

Only flattened output is ever written here — never the working Jaarvis vault.
"""
import argparse, base64, json, os, re, shutil, subprocess, sys, tempfile
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "_src", "manifest.json")
PAGES = "https://saurabh-labofone.github.io/onestop-share"

RASTER = {"png", "jpg", "jpeg", "gif", "webp", "avif"}
MIME = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "webp": "image/webp", "avif": "image/avif",
    "svg": "image/svg+xml", "css": "text/css", "js": "application/javascript",
    "woff2": "font/woff2", "woff": "font/woff", "ttf": "font/ttf",
}
# rasters bigger than this get downscaled to <=1440px long edge + JPEG q82
COMPRESS_OVER = 250_000
MAX_DIM = 1440
JPEG_Q = 82

# capture src="...", href="...", and url(...) targets
REF_RE = re.compile(r"""(?:src|href)\s*=\s*['"]([^'"]+)['"]|url\(\s*['"]?([^'")]+)['"]?\s*\)""", re.I)


def slugify(s):
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s or "page"


def is_local(ref):
    r = ref.strip()
    if not r or r.startswith(("http://", "https://", "data:", "#", "%23", "mailto:", "//", "tel:")):
        return False
    return True


def find_refs(html):
    refs = set()
    for m in REF_RE.finditer(html):
        ref = m.group(1) or m.group(2)
        if ref and is_local(ref):
            refs.add(ref.strip())
    return refs


def inline_one(ref, src_dir):
    """Return a data: URI for a local asset, or None if it can't be resolved."""
    path = os.path.normpath(os.path.join(src_dir, ref.split("?")[0].split("#")[0]))
    if not os.path.isfile(path):
        print(f"   ! missing, left as-is: {ref}", file=sys.stderr)
        return None
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    raw = open(path, "rb").read()
    if ext in RASTER and ext != "gif" and len(raw) > COMPRESS_OVER:
        # downscale + re-encode JPEG via macOS sips (no deps)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
            tmp = tf.name
        try:
            subprocess.run(
                ["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(JPEG_Q),
                 "-Z", str(MAX_DIM), path, "--out", tmp],
                check=True, capture_output=True,
            )
            comp = open(tmp, "rb").read()
            if comp and len(comp) < len(raw):
                b64 = base64.b64encode(comp).decode()
                print(f"   - {ref}: {len(raw)//1024}KB -> {len(comp)//1024}KB (jpeg)")
                return f"data:image/jpeg;base64,{b64}"
        except Exception as e:
            print(f"   ! compress failed ({ref}), inlining original: {e}", file=sys.stderr)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    mime = MIME.get(ext, "application/octet-stream")
    print(f"   - {ref}: {len(raw)//1024}KB ({mime}, inlined as-is)")
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def flatten(html, src_dir):
    refs = sorted(find_refs(html), key=len, reverse=True)  # longest first: avoids partial-path clobber
    for ref in refs:
        uri = inline_one(ref, src_dir)
        if uri:
            html = html.replace(ref, uri)
    # belt-and-braces noindex even though the whole site is robots-disallowed
    if "name=\"robots\"" not in html and "name='robots'" not in html:
        tag = '<meta name="robots" content="noindex,nofollow">'
        m = re.search(r"<head[^>]*>", html, re.I)
        html = (html[:m.end()] + "\n  " + tag + html[m.end():]) if m else tag + html
    return html


def load_manifest():
    try:
        return json.load(open(MANIFEST))
    except Exception:
        return {"items": []}


def save_manifest(man):
    json.dump(man, open(MANIFEST, "w"), indent=2)


def build_hub(man):
    items = sorted(man["items"], key=lambda x: x.get("updated", ""), reverse=True)
    cards = "\n".join(
        f"""      <a class="card" href="{it['slug']}/">
        <div class="t">{esc(it['title'])}</div>
        {f'<div class="b">{esc(it["blurb"])}</div>' if it.get('blurb') else ''}
        <div class="m">updated {it.get('updated','')[:10]} &middot; open &rarr;</div>
      </a>"""
        for it in items
    ) or '      <div class="empty">Nothing published yet.</div>'
    return HUB.replace("{{CARDS}}", cards).replace("{{N}}", str(len(items)))


def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def git(*args):
    return subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True)


def do_push(msg):
    git("add", "-A")
    c = git("commit", "-m", msg)
    if c.returncode != 0 and "nothing to commit" in (c.stdout + c.stderr):
        print("   (nothing changed)")
    p = git("push")
    if p.returncode != 0:
        print(p.stderr.strip() or "push failed", file=sys.stderr)
        print("   hint: set the remote/upstream once, then re-run with --push", file=sys.stderr)
    else:
        print("   pushed.")


def cmd_publish(a):
    src = os.path.abspath(a.in_path)
    if not os.path.isfile(src):
        sys.exit(f"input not found: {src}")
    slug = a.slug or slugify(a.title)
    print(f"publishing '{a.title}'  ->  /{slug}/")
    html = open(src, encoding="utf-8").read()
    html = flatten(html, os.path.dirname(src))
    out_dir = os.path.join(ROOT, slug)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    size = len(html.encode()) // 1024
    print(f"   wrote {slug}/index.html ({size}KB self-contained)")
    man = load_manifest()
    man["items"] = [x for x in man["items"] if x["slug"] != slug]
    man["items"].append({
        "slug": slug, "title": a.title, "blurb": a.blurb or "",
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    save_manifest(man)
    with open(os.path.join(ROOT, "index.html"), "w", encoding="utf-8") as f:
        f.write(build_hub(man))
    print(f"   hub regenerated ({len(man['items'])} item/s)")
    print(f"   link: {PAGES}/{slug}/")
    if a.push:
        do_push(f"publish: {slug}")


def cmd_remove(a):
    man = load_manifest()
    before = len(man["items"])
    man["items"] = [x for x in man["items"] if x["slug"] != a.remove]
    if len(man["items"]) == before:
        sys.exit(f"no such slug: {a.remove}")
    save_manifest(man)
    d = os.path.join(ROOT, a.remove)
    if os.path.isdir(d):
        shutil.rmtree(d)
    with open(os.path.join(ROOT, "index.html"), "w", encoding="utf-8") as f:
        f.write(build_hub(man))
    print(f"removed /{a.remove}/")
    if a.push:
        do_push(f"remove: {a.remove}")


def cmd_list(_a):
    man = load_manifest()
    if not man["items"]:
        print("nothing published yet.")
        return
    for it in sorted(man["items"], key=lambda x: x.get("updated", ""), reverse=True):
        print(f"  {it['slug']:<22} {it['updated'][:10]}  {PAGES}/{it['slug']}/")


HUB = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>OneStop &middot; review</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@500;600;700;800&display=swap" rel="stylesheet">
<style>
  :root{--ink:#0A2540;--cyan:#00B4D8;--paper:#F7FBFD;--line:#E3EEF4;--mut:#5B7488}
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:Manrope,system-ui,sans-serif;background:var(--paper);color:var(--ink);
       -webkit-font-smoothing:antialiased;padding:40px 20px 80px}
  .wrap{max-width:680px;margin:0 auto}
  .top{display:flex;align-items:baseline;gap:10px;margin-bottom:6px}
  .logo{font-weight:800;font-size:22px;letter-spacing:-.02em}
  .logo b{color:var(--cyan)}
  .tag{font-weight:600;font-size:13px;color:var(--mut)}
  .note{font-size:13px;color:var(--mut);margin:14px 0 28px;line-height:1.5;
        border-left:3px solid var(--cyan);padding:8px 0 8px 12px;background:#fff;border-radius:0 8px 8px 0}
  .card{display:block;text-decoration:none;color:inherit;background:#fff;border:1px solid var(--line);
        border-radius:14px;padding:18px 20px;margin-bottom:12px;transition:.15s ease}
  .card:hover{border-color:var(--cyan);transform:translateY(-1px);box-shadow:0 6px 22px rgba(0,180,216,.10)}
  .t{font-weight:700;font-size:17px;letter-spacing:-.01em}
  .b{font-size:14px;color:var(--mut);margin-top:3px;line-height:1.45}
  .m{font-size:12px;color:var(--cyan);font-weight:600;margin-top:10px}
  .empty{color:var(--mut);font-size:14px;padding:20px 0}
  .foot{margin-top:28px;font-size:12px;color:var(--mut)}
</style></head>
<body><div class="wrap">
  <div class="top"><div class="logo">ONE<b>STOP</b></div><div class="tag">internal review</div></div>
  <div class="note">Drafts for team review &mdash; not final. Open one, scan it on your phone, send notes back. The link stays the same; it updates as I revise.</div>
  {{CARDS}}
  <div class="foot">{{N}} item/s &middot; OneStop</div>
</div></body></html>"""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--title")
    p.add_argument("--in", dest="in_path")
    p.add_argument("--slug")
    p.add_argument("--blurb")
    p.add_argument("--push", action="store_true")
    p.add_argument("--list", action="store_true")
    p.add_argument("--remove")
    a = p.parse_args()
    if a.list:
        cmd_list(a)
    elif a.remove:
        cmd_remove(a)
    elif a.title and a.in_path:
        cmd_publish(a)
    else:
        p.error("need --title and --in (or --list / --remove)")


if __name__ == "__main__":
    main()
