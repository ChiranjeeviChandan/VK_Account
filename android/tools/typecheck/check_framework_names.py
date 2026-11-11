#!/usr/bin/env python3
"""Checks every android: attribute, framework style and enum/flag keyword used
in the app's XML against the names the Android 15 framework actually defines
(read from Robolectric's android-all jar: android.R$attr / android.R$style via
javap, keyword strings from resources.arsc). Catches typos that only aapt2
would otherwise report. Nothing from the jar is executed."""
import glob, os, re, subprocess, sys, zipfile
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
# usage: check_framework_names.py <android-all jar> [app/src/main directory]
if len(sys.argv) < 2:
    sys.exit("usage: check_framework_names.py <path to the android-all jar> [app/src/main]")
JAR = os.path.abspath(sys.argv[1])
APP = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else os.path.normpath(os.path.join(HERE, "..", "..", "app", "src", "main"))
ANDROID = "{http://schemas.android.com/apk/res/android}"

def fields(cls):
    out = subprocess.run(["javap", "-cp", JAR, cls], capture_output=True, text=True, timeout=120).stdout
    return set(re.findall(r"public static final int (\w+);", out))

attrs, styles = fields("android.R$attr"), fields("android.R$style")
arsc = zipfile.ZipFile(JAR).read("resources.arsc")
def keyword_known(word):
    return word.encode("utf-8") in arsc or word.encode("utf-16-le") in arsc

errors = []
files = sorted(glob.glob(os.path.join(APP, "res", "**", "*.xml"), recursive=True)) + [os.path.join(APP, "AndroidManifest.xml")]
KEYWORD_ATTRS = {"launchMode", "windowSoftInputMode", "foregroundServiceType", "accessibilityEventTypes",
                 "accessibilityFeedbackType", "accessibilityFlags"}
checked_attrs, checked_words = set(), set()
for path in files:
    for element in ET.parse(path).iter():
        for name, value in element.attrib.items():
            if name.startswith(ANDROID):
                attr = name[len(ANDROID):]
                checked_attrs.add(attr)
                if attr not in attrs:
                    errors.append(f"{path}: <{element.tag}> android:{attr} is not a framework attribute")
                if attr in KEYWORD_ATTRS:
                    for word in value.split("|"):
                        checked_words.add(word)
                        if not keyword_known(word):
                            errors.append(f"{path}: android:{attr}=\"{word}\" is not a keyword the framework defines")
            if name == "parent" and value.startswith("@android:style/"):
                style = value.split("/", 1)[1].replace(".", "_")
                if style not in styles:
                    errors.append(f"{path}: framework style {value} does not exist")
            if name == "name" and element.tag == "item" and value.startswith("android:"):
                checked_attrs.add(value[8:])
                if value[8:] not in attrs:
                    errors.append(f"{path}: theme item {value} is not a framework attribute")
print(f"framework defines {len(attrs)} attributes and {len(styles)} styles")
print(f"checked {len(checked_attrs)} distinct android: attributes and {len(checked_words)} keywords: {', '.join(sorted(checked_words))}")
if errors:
    print(f"{len(errors)} PROBLEM(S):")
    for e in errors: print("  -", e)
    sys.exit(1)
print("framework names OK")
