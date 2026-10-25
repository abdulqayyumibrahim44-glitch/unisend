[app]
title = Unisend
package.name = unisend
package.domain = org.unisend

source.dir = .
source.include_exts = py,png,jpg,kv,atlas
source.exclude_dirs = .github, bin, .buildozer

version = 1.0
requirements = python3,kivy==2.3.0,pyjnius,android,qrcode,typing_extensions

orientation = portrait
fullscreen = 0

# Uncomment after adding a square icon.png (512x512) next to main.py:
#icon.filename = %(source.dir)s/icon.png

android.permissions = INTERNET,ACCESS_NETWORK_STATE,ACCESS_WIFI_STATE,CHANGE_WIFI_MULTICAST_STATE,WAKE_LOCK,READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE,MANAGE_EXTERNAL_STORAGE
android.api = 33
android.minapi = 24
android.archs = arm64-v8a, armeabi-v7a
android.accept_sdk_license = True
android.enable_androidx = True

[buildozer]
log_level = 2
warn_on_root = 0
