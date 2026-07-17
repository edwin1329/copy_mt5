@echo off
setlocal enabledelayedexpansion
title CopyMT5

cd /d "%~dp0"

echo.
echo  ============================================
echo   CopyMT5 - Replicador de operaciones MT5
echo  ============================================
echo.

:: ── Ruta local para el entorno virtual ───────────────────────────────────────
:: El proyecto puede estar en una unidad de red (carpeta compartida UTM),
:: pero el venv SIEMPRE se crea en una ruta local para que pip funcione.
set VENV_LOCAL=C:\Projects\copy_mt5\.venv

:: ── 1. Verificar Python ───────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python no encontrado.
    echo  Instala Python 3.11 64-bit desde python.org y marca "Add to PATH".
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PY_VER=%%v
for /f "tokens=1,2 delims=." %%a in ("!PY_VER!") do (
    set PY_MAJOR=%%a
    set PY_MINOR=%%b
)

echo  Python !PY_VER! detectado.

if !PY_MAJOR! GTR 3 goto :version_error
if !PY_MINOR! GTR 12 (
    echo  [AVISO] Python !PY_VER! puede no ser compatible con MetaTrader5.
    echo  Se recomienda Python 3.11 64-bit.
    echo.
    echo  Continuando en 5 segundos...
    timeout /t 5 /nobreak >nul
)
goto :after_version

:version_error
echo  [ERROR] Python !PY_VER! no compatible. Usa Python 3.11 64-bit.
pause
exit /b 1

:after_version

:: ── 2. Entorno virtual en ruta local ─────────────────────────────────────────
if exist "%VENV_LOCAL%\Scripts\activate.bat" (
    :: Verificar que pip funciona
    "%VENV_LOCAL%\Scripts\python.exe" -m pip --version >nul 2>&1
    if errorlevel 1 (
        echo  Entorno virtual danado, recreando en %VENV_LOCAL%...
        rmdir /s /q "%VENV_LOCAL%"
    )
)

if not exist "%VENV_LOCAL%\Scripts\activate.bat" (
    echo  Creando entorno virtual en %VENV_LOCAL%...
    if not exist "C:\Projects\copy_mt5" mkdir "C:\Projects\copy_mt5"
    python -m venv "%VENV_LOCAL%"
    if errorlevel 1 (
        echo  [ERROR] No se pudo crear el entorno virtual.
        pause
        exit /b 1
    )
    echo  Entorno virtual creado.
)

call "%VENV_LOCAL%\Scripts\activate.bat"

:: ── 3. Instalar / actualizar dependencias ────────────────────────────────────
echo  Actualizando pip...
python -m pip install --upgrade pip setuptools wheel --quiet --disable-pip-version-check

echo  Verificando dependencias...
python -m pip install -r "%~dp0requirements.txt" --disable-pip-version-check
if errorlevel 1 (
    echo.
    echo  [ERROR] Fallo al instalar dependencias. Ver error arriba.
    pause
    exit /b 1
)
echo  Dependencias OK.

:: ── 4. Verificar config.json ─────────────────────────────────────────────────
if not exist "%~dp0config.json" (
    echo.
    echo  [AVISO] No se encontro config.json.
    copy "%~dp0config.json.example" "%~dp0config.json" >nul
    echo  Completa los datos de tus cuentas MT5 y vuelve a ejecutar run.bat.
    echo.
    notepad "%~dp0config.json"
    pause
    exit /b 0
)

:: ── 5. Abrir terminales MT5 ───────────────────────────────────────────────────
echo.
echo  Abriendo terminales MT5...

:: Ruta del Master (leida desde config.json)
for /f "usebackq delims=" %%p in (`powershell -NoProfile -Command "(Get-Content '%~dp0config.json' | ConvertFrom-Json).master.path"`) do set MASTER_PATH=%%p

if defined MASTER_PATH (
    if exist "!MASTER_PATH!" (
        start "" "!MASTER_PATH!"
        echo  [OK] Master: !MASTER_PATH!
    ) else (
        echo  [AVISO] No se encontro el ejecutable del Master: !MASTER_PATH!
    )
) else (
    echo  [AVISO] No se pudo leer la ruta del Master en config.json
)

:: Rutas de los Followers (leidas desde config.json)
for /f "usebackq delims=" %%p in (`powershell -NoProfile -Command "(Get-Content '%~dp0config.json' | ConvertFrom-Json).followers | ForEach-Object { $_.path }"`) do (
    if exist "%%p" (
        start "" "%%p"
        echo  [OK] Follower: %%p
    ) else (
        echo  [AVISO] No se encontro el ejecutable del Follower: %%p
    )
)

echo.
echo  Recuerda adjuntar el EA TradeSignaler al master (para modo evento).
echo  Esperando 10 segundos para que los terminales inicien...
timeout /t 10 /nobreak >nul

:: ── 6. Ejecutar desde la carpeta del proyecto (red o local) ──────────────────
echo.
python "%~dp0main.py"

:: ── 7. Fin ────────────────────────────────────────────────────────────────────
echo.
echo  CopyMT5 detenido.
pause
