"""Static checks for the Monkey C sources: delimiter balance, resource
references, and cross-file symbol resolution."""
import re
import pathlib
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parent.parent / "watch"
SRC = sorted((ROOT / "source").rglob("*.mc"))
fails = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        fails.append(label)


def strip(code):
    """Remove comments and string literals so delimiters inside them don't count."""
    code = re.sub(r'"(\\.|[^"\\])*"', '""', code)
    code = re.sub(r"//.*", "", code)
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    return code


print("-- delimiter balance --")
for p in SRC:
    code = strip(p.read_text())
    for open_c, close_c, name in (("{", "}", "braces"), ("(", ")", "parens"), ("[", "]", "brackets")):
        check(f"{p.name} {name} balanced",
              code.count(open_c) == code.count(close_c),
              f"{code.count(open_c)} open vs {code.count(close_c)} close")

print("\n-- resource references resolve --")
strings = {s.get("id") for s in ET.parse(ROOT / "resources/strings/strings.xml").getroot()}
drawables = {d.get("id") for d in ET.parse(ROOT / "resources/drawables/drawables.xml").getroot()}
props = {p.get("id") for p in ET.parse(ROOT / "resources/properties/properties.xml").getroot()}

used_strings, used_drawables = set(), set()
for p in SRC:
    text = p.read_text()
    used_strings |= set(re.findall(r"Rez\.Strings\.(\w+)", text))
    used_drawables |= set(re.findall(r"Rez\.Drawables\.(\w+)", text))

missing = used_strings - strings
check("every Rez.Strings.* exists in strings.xml", not missing, f"missing: {sorted(missing)}")
missing_d = used_drawables - drawables
check("every Rez.Drawables.* exists in drawables.xml", not missing_d, f"missing: {sorted(missing_d)}")

unused = strings - used_strings
print(f"  note: {len(unused)} string(s) defined but unused: {sorted(unused)}" if unused else "  note: all strings used")

# settings.xml must reference only declared properties
settings_props = set(re.findall(r"@Properties\.(\w+)", (ROOT / "resources/settings/settings.xml").read_text()))
check("settings.xml references only declared properties", settings_props <= props,
      f"undeclared: {sorted(settings_props - props)}")

# Config reads properties by string name
config_props = set(re.findall(r'getProp\("(\w+)"', (ROOT / "source/Config.mc").read_text()))
check("Config.mc reads only declared properties", config_props <= props,
      f"undeclared: {sorted(config_props - props)}")

print("\n-- class / symbol resolution --")
defined = set()
for p in SRC:
    text = p.read_text()
    defined |= set(re.findall(r"^class\s+(\w+)", text, re.M))
    defined |= set(re.findall(r"^module\s+(\w+)", text, re.M))

for name in ("PodcastApp", "PodcastSyncDelegate", "PodcastContentDelegate",
             "PodcastIterator", "BrowseMenu", "BrowseMenuDelegate",
             "ResumeMenu", "ResumeMenuDelegate", "SyncConfigMenu",
             "SyncConfigMenuDelegate", "Config", "Store", "Util"):
    check(f"{name} is defined", name in defined)

# Every `new Foo(` must refer to a class we define or a Toybox one.
instantiated = set()
for p in SRC:
    instantiated |= set(re.findall(r"new\s+([A-Z]\w*)\s*\(", p.read_text()))
unknown = {c for c in instantiated if c not in defined}
check("all locally-instantiated classes are defined", not unknown, f"unknown: {sorted(unknown)}")

print("\n-- manifest --")
manifest = ET.parse(ROOT / "manifest.xml").getroot()
ns = {"iq": "http://www.garmin.com/xml/connectiq"}
app = manifest.find("iq:application", ns)
check("app type is audio-content-provider-app", app.get("type") == "audio-content-provider-app")
check("entry class matches a defined class", app.get("entry") in defined, app.get("entry"))
check("app id is 32 hex chars", bool(re.fullmatch(r"[0-9a-f]{32}", app.get("id"))), app.get("id"))
check("Communications permission declared",
      any(u.get("id") == "Communications" for u in app.find("iq:permissions", ns)))
products = [p.get("id") for p in app.find("iq:products", ns)]
check("at least one product listed", len(products) > 0)
print(f"  note: {len(products)} products targeted")

print("\n-- Store field offsets are consistent --")
store = (ROOT / "source/Store.mc").read_text()
enum_body = re.search(r"enum\s*\{(.*?)\}", store, re.S).group(1)
offsets = dict(re.findall(r"(F_\w+)\s*=\s*(\d+)", enum_body))
check("five field offsets declared", len(offsets) == 5, str(offsets))
check("offsets are 0..4 with no gaps", sorted(int(v) for v in offsets.values()) == [0, 1, 2, 3, 4])
add_args = re.search(r"lib\[systemId\]\s*=\s*\[(.*?)\];", store, re.S).group(1)
check("Store.add writes exactly 5 fields", len(add_args.split(",")) == 5, add_args.strip())

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURE(S): {fails}"))
raise SystemExit(1 if fails else 0)
