plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
}

android {
    namespace = "io.github.adityakamath255.todo"
    compileSdk = 37

    defaultConfig {
        applicationId = "io.github.adityakamath255.todo"
        // java.time arrived in API 26
        minSdk = 26
        targetSdk = 37
        versionCode = 1
        versionName = "0.1"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        compose = true
    }
}

dependencies {
    implementation(project(":model"))
    implementation(platform(libs.compose.bom))
    implementation(libs.activity.compose)
}
