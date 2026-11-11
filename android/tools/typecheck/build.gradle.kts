import org.jetbrains.kotlin.gradle.dsl.JvmTarget

// Checks the companion app WITHOUT the Android SDK or the Android Gradle plugin:
//   - resources and manifest (check_resources.py, check_framework_names.py)
//   - every Kotlin source, compiled against the Android 15 framework classes
//     published on Maven Central as org.robolectric:android-all
//   - the JVM unit tests
// It does not produce an APK. See README.md in this folder.
plugins {
    kotlin("jvm") version "2.3.21"
}

repositories {
    mavenCentral()
}

val appSources = rootDir.resolve("../../app/src")
val generatedR = layout.buildDirectory.dir("generated/r")

// The framework jar also contains hidden platform APIs, so a clean compile here
// is necessary but not sufficient: the real SDK build in CI has the last word.
val framework: Configuration by configurations.creating

dependencies {
    framework("org.robolectric:android-all:15-robolectric-14663667-2")
    compileOnly(files(framework))
    testImplementation("junit:junit:4.13.2")
}

val checkResources by tasks.registering(Exec::class) {
    description = "Validates resources and the manifest and generates a stub R class."
    inputs.dir(appSources.resolve("main/res"))
    inputs.dir(appSources.resolve("main/java"))
    inputs.file(appSources.resolve("main/AndroidManifest.xml"))
    inputs.file("check_resources.py")
    outputs.dir(generatedR)
    commandLine("python3", "check_resources.py", appSources.resolve("main").path, generatedR.get().asFile.path)
}

val checkFrameworkNames by tasks.registering(Exec::class) {
    description = "Checks android: attributes, styles and keywords used in XML against the framework."
    inputs.dir(appSources.resolve("main/res"))
    inputs.file(appSources.resolve("main/AndroidManifest.xml"))
    inputs.file("check_framework_names.py")
    inputs.files(framework)
    doFirst {
        commandLine("python3", "check_framework_names.py", framework.singleFile.path, appSources.resolve("main").path)
    }
}

sourceSets {
    main {
        kotlin.setSrcDirs(listOf(appSources.resolve("main/java"), generatedR))
        java.setSrcDirs(listOf(generatedR))
    }
    test {
        kotlin.setSrcDirs(listOf(appSources.resolve("test/java")))
    }
}

tasks.named("compileKotlin") { dependsOn(checkResources) }
tasks.named("compileJava") { dependsOn(checkResources) }
tasks.named("check") { dependsOn(checkFrameworkNames) }

kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
    }
}

java {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}

tasks.test {
    useJUnit()
    testLogging {
        events("failed", "skipped")
        exceptionFormat = org.gradle.api.tasks.testing.logging.TestExceptionFormat.FULL
    }
}
