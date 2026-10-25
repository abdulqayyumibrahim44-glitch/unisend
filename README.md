# Unisend for Android: build the APK

You do not need Android Studio or Linux. GitHub builds the APK for you, free, in the cloud.
This takes about 20 to 40 minutes the first time.

## Steps

1. Make a free account at https://github.com and create a **new repository** (any name, Public or Private).
2. Upload everything in this folder to the repository, keeping the same layout:
   - `main.py`
   - `unisend_core.py`
   - `buildozer.spec`
   - `.github/workflows/build-apk.yml`  (the `.github` folder is hidden on some computers)

   Easiest way: on the repository page choose **Add file → Upload files** and drag the files in.
   If the `.github` folder will not upload, choose **Add file → Create new file**, type
   `.github/workflows/build-apk.yml` as the name, and paste the contents of that file.
3. Open the **Actions** tab, click **Build APK** on the left, then **Run workflow**.
4. Wait for the green tick. Open the finished run and download **Unisend-apk** at the bottom
   (it is a zip containing the `.apk`).
5. Unzip it, send the `.apk` to your phone, and open it. Android will ask you to allow
   installing from this source; allow it. Play Protect may warn that the app is unknown
   (it is not signed for the Play Store): choose **Install anyway**.

## First run on the phone

- Allow the storage permission when asked.
- Android 11 and newer also asks you to turn on **All files access** for Unisend (a Settings page
  opens). Without it, Unisend can only share its own private folder.
- Keep Unisend open while sharing, and set the phone's battery setting for Unisend to
  **Unrestricted**, or Android may pause it.

## Using it

- The other person opens the link shown (or scans the QR code) in a browser.
- If they also have Unisend, they appear under **Nearby devices**. Tap one, then
  **Send files to selected...**.
- Phones and PCs both work: the PC version (`unisend.py`) and this app find each other on the same Wi-Fi.

## Notes

- To change the app, edit `main.py`. The sharing engine in `unisend_core.py` is a copy of `unisend.py`;
  if you change `unisend.py`, copy it over `unisend_core.py` again and rebuild.
- To add your icon: put a square `icon.png` next to `main.py`, remove the `#` in front of
  `icon.filename` in `buildozer.spec`, and rebuild.
- If the build fails, open the failed run, copy the red error lines near the bottom, and ask for help.
  First builds of Android apps often need one small fix.
- This is a debug build for installing by hand. Publishing on the Google Play Store needs a signed
  release build, and Google restricts the 'All files access' permission there.
