# Checking the app without the Android SDK

The real build (`./gradlew assembleDebug`) needs the Android SDK and Google's
Maven repository. Where those are not available - a locked-down CI box, a
cloud coding session - this small project still catches most mistakes:

| Step | What it proves |
|---|---|
| `check_resources.py` | Every XML file is well-formed; every `@type/name` and `R.type.name` reference resolves; strings are free of the mistakes `aapt2` rejects; manifest components exist and declare `android:exported`; permissions used in code are declared |
| `check_framework_names.py` | Every `android:` attribute, framework style and enum keyword used in XML exists in the Android 15 framework |
| `compileKotlin` | All app sources type-check against the Android 15 framework classes (`org.robolectric:android-all` from Maven Central) |
| `test` | The JVM unit tests pass |

Run it with any Gradle 8.x (or the app's wrapper) and Python 3:

```bash
gradle -p tools/typecheck check
```

The first run downloads the framework jar (about 190 MB).

## What it cannot prove

* No APK is built: `aapt2`, the manifest merger, D8 and packaging do not run.
* `android-all` contains hidden platform APIs that the public SDK does not
  offer. Code that uses one compiles here and fails in the real build. Stick to
  documented APIs; the GitHub Actions build is the final check.
* Nothing runs on a device.
