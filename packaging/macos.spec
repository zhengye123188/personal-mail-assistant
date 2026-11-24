from pathlib import Path


packaging_dir = Path(SPECPATH)
analysis = Analysis(
    [str(packaging_dir / "macos_launcher.py")],
    pathex=[str(packaging_dir.parent)],
)
python_modules = PYZ(analysis.pure)
executable = EXE(
    python_modules,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="PersonalMailAssistant",
    console=False,
    upx=False,
)
app = BUNDLE(
    executable,
    analysis.binaries,
    analysis.datas,
    name="个人邮件助手.app",
    icon=str(packaging_dir / "maildesk.icns"),
    bundle_identifier="io.github.zhengye123188.personal-mail-assistant",
    version="0.1.2",
    info_plist={
        "CFBundleDisplayName": "个人邮件助手",
        "CFBundleDevelopmentRegion": "zh_CN",
        "CFBundleVersion": "3",
        # The arm64 Qt 6.12 bindings included in this build target macOS 26.
        "LSMinimumSystemVersion": "26.0",
        "LSApplicationCategoryType": "public.app-category.productivity",
        "NSHighResolutionCapable": True,
    },
)
