# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for agent-mesh edge agent."""
from pathlib import Path

project_root = Path(SPECPATH).parent.resolve()
src_root = project_root / "src"

a = Analysis(
    [str(src_root / "agent_mesh" / "edge" / "agent.py")],
    pathex=[str(src_root)],
    binaries=[],
    datas=[
        (str(src_root / "agent_mesh" / "orchestrator" / "store" / "migrations"), "agent_mesh/orchestrator/store/migrations"),
    ],
    hiddenimports=[
        "uvicorn",
        "uvicorn.protocols.http.httptools_impl",
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.loops.uvloop",
        "httpx",
        "pydantic",
        "pydantic_settings",
        "yaml",
        "psutil",
        "passlib.handlers.bcrypt",
        "agent_mesh.edge.agent",
        "agent_mesh.edge.config_writer",
        "agent_mesh.edge.execution",
        "agent_mesh.edge.execution.common",
        "agent_mesh.edge.execution.command",
        "agent_mesh.edge.execution.llm",
        "agent_mesh.edge.rest_client",
        "agent_mesh.orchestrator.config",
        "agent_mesh.orchestrator.auth",
        "agent_mesh.orchestrator.store.sqlite",
        "agent_mesh.shared.constants",
        "agent_mesh.shared.schemas",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="agent-mesh-edge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
