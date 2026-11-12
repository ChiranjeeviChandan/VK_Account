#!/usr/bin/env python3
"""Stand-in for aapt2 + manifest merger checks, and generator of a stub R class.

Verifies, without the Android SDK:
  * every XML file is well-formed;
  * every @type/name reference in XML and every R.type.name reference in Kotlin
    resolves to a resource defined in app/src/main/res;
  * string resources are free of the mistakes aapt2 rejects (unescaped
    apostrophes, non-positional multiple format arguments, bad names);
  * resources in qualified folders also exist in the default configuration;
  * manifest components exist as classes and declare android:exported.
Then writes a stub ai/felix/companion/R.java so the Kotlin sources can be
type-checked outside Gradle's Android plugin.
"""
import os, re, sys, glob
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
# usage: check_resources.py [app/src/main directory] [directory for the generated R.java]
APP = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.normpath(os.path.join(HERE, "..", "..", "app", "src", "main"))
OUT = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else os.path.join(HERE, "build", "generated", "r")
RES = os.path.join(APP, "res")
SRC = os.path.join(APP, "java")
PKG = "ai.felix.companion"
ANDROID = "{http://schemas.android.com/apk/res/android}"
errors, notes = [], []

def err(msg): errors.append(msg)

# ---- 1. well-formedness ---------------------------------------------------
xml_files = sorted(glob.glob(os.path.join(RES, "**", "*.xml"), recursive=True)) + [os.path.join(APP, "AndroidManifest.xml")]
trees = {}
for path in xml_files:
    try:
        trees[path] = ET.parse(path)
    except ET.ParseError as exc:
        err(f"{path}: not well-formed XML: {exc}")

# ---- 2. defined resources -------------------------------------------------
defined = {}            # type -> {name: [config, ...]}
def define(rtype, name, config):
    defined.setdefault(rtype, {}).setdefault(name, []).append(config)

VALUE_TAGS = {"string": "string", "color": "color", "dimen": "dimen", "bool": "bool", "integer": "integer",
              "style": "style", "string-array": "array", "integer-array": "array", "array": "array", "plurals": "plurals"}
strings = {}
for path, tree in trees.items():
    rel = os.path.relpath(path, RES)
    if rel.startswith(".."):
        continue
    folder = rel.split(os.sep)[0]
    base, _, qualifiers = folder.partition("-")
    config = qualifiers or "default"
    if base == "values":
        root = tree.getroot()
        if root.tag != "resources":
            err(f"{path}: root element must be <resources>")
            continue
        for child in root:
            name = child.get("name")
            if child.tag == "item":
                rtype = child.get("type")
            else:
                rtype = VALUE_TAGS.get(child.tag)
            if rtype is None or name is None:
                err(f"{path}: unsupported or unnamed element <{child.tag}>")
                continue
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", name):
                err(f"{path}: invalid resource name {name!r}")
            if config in defined.get(rtype, {}).get(name.replace(".", "_"), []):
                err(f"{path}: duplicate {rtype}/{name} in configuration {config}")
            define(rtype, name.replace(".", "_"), config)
            if rtype == "string":
                strings[(name, config)] = ("".join(child.itertext()), path)
    else:
        name = os.path.splitext(os.path.basename(path))[0]
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            err(f"{path}: file-based resource names must be lower-case letters, digits and underscores")
        define(base, name, config)

for path in glob.glob(os.path.join(RES, "**", "*.*"), recursive=True):
    if not path.endswith(".xml"):
        folder = os.path.relpath(path, RES).split(os.sep)[0]
        base, _, qualifiers = folder.partition("-")
        define(base, os.path.splitext(os.path.basename(path))[0], qualifiers or "default")

# ---- 3. references in XML -------------------------------------------------
REF = re.compile(r"^@(?:(\+)?)(?:([a-zA-Z0-9_.]+):)?([a-z]+)/([A-Za-z0-9_.]+)$")
def check_ref(value, where):
    m = REF.match(value)
    if not m:
        if value.startswith("@") and value not in ("@null", "@empty"):
            err(f"{where}: unparseable resource reference {value!r}")
        return
    plus, package, rtype, name = m.groups()
    if package == "android":
        return
    if plus:
        define(rtype, name, "default")
        return
    if name.replace(".", "_") not in defined.get(rtype, {}):
        err(f"{where}: {value} is not defined")

for path, tree in trees.items():
    for element in tree.iter():
        for attr, value in element.attrib.items():
            if value.startswith("@"):
                check_ref(value, f"{path} <{element.tag}> {attr.replace(ANDROID, 'android:')}")
            if attr == "parent" and value and not value.startswith("@") and not value.startswith("android:"):
                if value.replace(".", "_") not in defined.get("style", {}):
                    err(f"{path}: parent style {value} is not defined")
        text = (element.text or "").strip()
        if text.startswith("@") and element.tag in ("item", "color", "string", "dimen"):
            check_ref(text, f"{path} <{element.tag}> text")

# ---- 4. references in Kotlin ----------------------------------------------
used = set()
for path in glob.glob(os.path.join(SRC, "**", "*.kt"), recursive=True):
    source = open(path, encoding="utf-8").read()
    for rtype, name in re.findall(r"\bR\.([a-z]+)\.([A-Za-z0-9_]+)", source):
        used.add((rtype, name))
        if name not in defined.get(rtype, {}):
            err(f"{os.path.relpath(path, SRC)}: R.{rtype}.{name} is not defined")
    if re.search(r"\bandroidx\.|\bcom\.google\.android\.material", source):
        err(f"{os.path.relpath(path, SRC)}: uses a library outside the Android framework")
    if re.search(r"^import org\.json\.", source, re.M):
        err(f"{os.path.relpath(path, SRC)}: org.json is not used in this project (use core.Json)")

# ---- 5. string hygiene ----------------------------------------------------
for (name, _config), (text, path) in strings.items():
    body = text.strip()
    quoted = len(body) >= 2 and body[0] == '"' and body[-1] == '"'
    if not quoted:
        for m in re.finditer(r"(?<!\\)'", body):
            err(f"{path}: string {name}: apostrophe at {m.start()} must be written \\'")
        if re.search(r'(?<!\\)"', body):
            err(f"{path}: string {name}: double quote must be escaped")
    if body.startswith("?") or body.startswith("@"):
        err(f"{path}: string {name}: leading {body[0]} must be escaped")
    specs = re.findall(r"%(\d+\$)?[-#+ 0,(]*\d*(?:\.\d+)?([a-zA-Z%])", body)
    real = [s for s in specs if s[1] != "%"]
    if len(real) > 1 and any(not s[0] for s in real):
        err(f"{path}: string {name}: several format arguments must be positional (%1$s, %2$d)")
    if re.search(r"%(?![\d%a-zA-Z])", body):
        err(f"{path}: string {name}: stray % sign")
    positions = sorted({int(s[0][:-1]) for s in real if s[0]})
    if positions and positions != list(range(1, len(positions) + 1)):
        err(f"{path}: string {name}: format positions {positions} are not contiguous from 1")

# ---- 6. qualified resources need a default --------------------------------
for rtype, names in defined.items():
    for name, configs in names.items():
        if "default" not in configs:
            minimal = all(re.fullmatch(r"(anydpi-)?v\d+", c) and int(re.search(r"v(\d+)", c).group(1)) <= 26 for c in configs)
            if not minimal:
                err(f"{rtype}/{name} exists only for {configs}; devices outside those get no value")

# ---- 7. format-argument use in code ---------------------------------------
default_strings = {name: text for (name, config), (text, _) in strings.items() if config == "default"}
for path in glob.glob(os.path.join(SRC, "**", "*.kt"), recursive=True):
    source = open(path, encoding="utf-8").read()
    for m in re.finditer(r"getString\(\s*R\.string\.([A-Za-z0-9_]+)\s*(,|\))", source):
        name, tail = m.group(1), m.group(2)
        text = default_strings.get(name, "")
        expects = len({s[0] for s in re.findall(r"%(\d+\$)[-#+ 0,(]*\d*(?:\.\d+)?([a-zA-Z])", text)})
        if tail == ")" and expects:
            err(f"{os.path.relpath(path, SRC)}: getString(R.string.{name}) needs {expects} format argument(s)")
        if tail == "," and not expects:
            err(f"{os.path.relpath(path, SRC)}: getString(R.string.{name}, ...) passes arguments to a string without placeholders")

# ---- 8. manifest ----------------------------------------------------------
manifest = trees.get(os.path.join(APP, "AndroidManifest.xml"))
classes = {}
for path in glob.glob(os.path.join(SRC, "**", "*.kt"), recursive=True):
    source = open(path, encoding="utf-8").read()
    package = re.search(r"^package\s+([\w.]+)", source, re.M).group(1)
    for cls in re.findall(r"^(?:open |abstract |private |internal )*class\s+(\w+)", source, re.M):
        classes[f"{package}.{cls}"] = source
if manifest is not None:
    root = manifest.getroot()
    if root.get("package"):
        err("AndroidManifest.xml: the package attribute must not be set (namespace lives in build.gradle.kts)")
    app = root.find("application")
    bases = {"activity": "Activity", "service": "Service", "receiver": "BroadcastReceiver", "provider": "ContentProvider"}
    for kind in bases:
        for comp in app.findall(kind):
            name = comp.get(ANDROID + "name")
            full = PKG + name if name.startswith(".") else name
            if full not in classes:
                err(f"AndroidManifest.xml: <{kind}> {name}: class {full} does not exist")
            if comp.find("intent-filter") is not None and comp.get(ANDROID + "exported") is None:
                err(f"AndroidManifest.xml: <{kind}> {name} has an intent filter and must declare android:exported")
            if kind == "service" and comp.get(ANDROID + "foregroundServiceType") == "specialUse":
                props = [p.get(ANDROID + "name") for p in comp.findall("property")]
                if "android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE" not in props:
                    err(f"AndroidManifest.xml: {name}: specialUse services need PROPERTY_SPECIAL_USE_FGS_SUBTYPE")
    declared = {p.get(ANDROID + "name") for p in root.findall("uses-permission")}
    all_source = "\n".join(classes.values())
    for perm in sorted(set(re.findall(r"Manifest\.permission\.([A-Z_]+)", all_source))):
        if f"android.permission.{perm}" not in declared:
            err(f"AndroidManifest.xml: code uses Manifest.permission.{perm} but the manifest does not declare it")
    for needed, why in (("android.permission.FOREGROUND_SERVICE", "foreground service"),
                        ("android.permission.FOREGROUND_SERVICE_SPECIAL_USE", "specialUse foreground service"),
                        ("android.permission.INTERNET", "network"),
                        ("android.permission.SYSTEM_ALERT_WINDOW", "overlay")):
        if needed not in declared:
            err(f"AndroidManifest.xml: missing {needed} ({why})")

# ---- unused resources (information only) ----------------------------------
xml_text = "\n".join(open(p, encoding="utf-8").read() for p in xml_files)
for rtype, names in sorted(defined.items()):
    for name in sorted(names):
        if (rtype, name) not in used and f"@{rtype}/{name.replace('_', '.')}" not in xml_text and f"@{rtype}/{name}" not in xml_text:
            notes.append(f"unused: {rtype}/{name}")

# ---- 9. stub R ------------------------------------------------------------
out_dir = os.path.join(OUT, *PKG.split("."))
os.makedirs(out_dir, exist_ok=True)
lines = [f"package {PKG};", "", "/** Generated by check_resources.py for local type-checking only. */", "public final class R {"]
value = 0x7f000000
for rtype in sorted(defined):
    lines.append(f"    public static final class {rtype} {{")
    for name in sorted(defined[rtype]):
        value += 1
        lines.append(f"        public static final int {name} = 0x{value:08x};")
    lines.append("    }")
lines.append("}")
open(os.path.join(out_dir, "R.java"), "w").write("\n".join(lines) + "\n")

print(f"checked {len(xml_files)} XML files, {sum(len(v) for v in defined.values())} resources, {len(used)} code references")
for note in notes:
    print("note:", note)
if errors:
    print(f"\n{len(errors)} PROBLEM(S):")
    for e in errors:
        print("  -", e)
    sys.exit(1)
print("resources OK")
