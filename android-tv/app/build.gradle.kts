// Atlas for Android TV: a full-screen WebView around the TV page Atlas Mobile serves (/tv).
// Release signing comes from the environment (the GitHub workflow decodes it from repository secrets):
//   ANDROID_KEYSTORE_PATH, ANDROID_KEYSTORE_PASSWORD, ANDROID_KEY_ALIAS, ANDROID_KEY_PASSWORD
// Without them a release build is signed with the debug key, so a test build still installs.
plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

val atlasVersion = (findProperty("atlasVersion") as String?)?.removePrefix("v")?.takeIf { it.isNotBlank() } ?: "1.0.0"
val versionParts = atlasVersion.substringBefore('-').split('.').map { it.toIntOrNull() ?: 0 } + listOf(0, 0, 0)
val atlasVersionCode = versionParts[0] * 10000 + versionParts[1] * 100 + versionParts[2]

android {
    namespace = "com.phonehseng.atlas.tv"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.phonehseng.atlas.tv"
        minSdk = 21
        targetSdk = 35
        versionCode = maxOf(1, atlasVersionCode)
        versionName = atlasVersion
    }

    signingConfigs {
        val ks = System.getenv("ANDROID_KEYSTORE_PATH")
        if (!ks.isNullOrBlank() && file(ks).exists()) {
            create("release") {
                storeFile = file(ks)
                storePassword = System.getenv("ANDROID_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("ANDROID_KEY_ALIAS")
                keyPassword = System.getenv("ANDROID_KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.findByName("release") ?: signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    lint {
        abortOnError = false
        checkReleaseBuilds = false
    }
}
