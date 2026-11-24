from pathlib import Path


packaging_dir = Path(SPECPATH)
analysis = Analysis(
    [str(packaging_dir / "windows_launcher.py")],
    pathex=[str(packaging_dir.parent)],
)
executable = EXE(
    PYZ(analysis.pure),
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="PersonalMailAssistant",
    icon=str(packaging_dir / "maildesk.ico"),
    console=False,
    upx=False,
)
app = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    name="PersonalMailAssistant",
    upx=False,
)
