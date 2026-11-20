@echo off
rem ============================================================
rem  Monitor OBD2 - Citroen C3 (somente leitura)
rem  Uso:  iniciar.bat                 carro real (Bluetooth LE)
rem        iniciar.bat --sim           simulador, sem carro
rem        iniciar.bat --sim --cenario superaquecimento --fator 10
rem        iniciar.bat --selftest      autoteste no carro parado
rem        iniciar.bat --benchmark     benchmark de velocidade (carro PARADO, motor ligado)
rem        iniciar.bat --benchmark --sim   benchmark no simulador
rem ============================================================
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title Monitor OBD2
cd /d "%~dp0"

echo.
echo   Monitor OBD2 - Citroen C3 - somente leitura
echo   -------------------------------------------
echo.

rem ---------- 1) Python ----------
call :acha_python
if not defined PYEXE (
    echo Python 3.10 a 3.13 nao encontrado. Instalando o Python 3.12 com o winget...
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    call :acha_python
)
if not defined PYEXE (
    echo.
    echo [ERRO] Nao consegui encontrar nem instalar o Python.
    echo Instale manualmente com:  winget install Python.Python.3.12
    echo e rode este arquivo de novo.
    goto :erro
)
echo Python: !PYEXE! !PYARG!

rem ---------- 2) Onde fica o ambiente virtual ----------
rem  Padrao: %LOCALAPPDATA%\obd-monitor\venv (fora do OneDrive, para nao sincronizar
rem  centenas de MB). Se esse disco tiver menos de 2 GB livres, usa a unidade com mais
rem  espaco. A escolha fica guardada em venv_local.txt.
set "VENV="
if defined OBDMON_VENV set "VENV=%OBDMON_VENV%"
if not defined VENV if exist "%~dp0.venv\Scripts\python.exe" set "VENV=%~dp0.venv"
if not defined VENV if exist "%~dp0venv_local.txt" set /p VENV=<"%~dp0venv_local.txt"
if not defined VENV (
    set "DEST=LOCAL"
    for /f "usebackq delims=" %%L in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$f=(Get-PSDrive -Name ($env:LOCALAPPDATA[0])).Free; if ($f -ge 2GB) {'LOCAL'} else { $b = Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | Sort-Object FreeSpace -Descending | Select-Object -First 1; if ($b.FreeSpace -ge 1GB) {$b.DeviceID} else {'NONE'} }"`) do set "DEST=%%L"
    if "!DEST!"=="LOCAL" (
        set "VENV=%LOCALAPPDATA%\obd-monitor\venv"
    ) else if "!DEST!"=="NONE" (
        echo [AVISO] Pouco espaco livre em disco. Vou tentar mesmo assim.
        set "VENV=%LOCALAPPDATA%\obd-monitor\venv"
    ) else (
        set "VENV=!DEST!\obd-monitor-venv-app"
        echo [AVISO] Pouco espaco livre no disco do sistema - o ambiente do Python vai ficar em !VENV!
    )
    > "%~dp0venv_local.txt" echo !VENV!
)

rem ---------- 3) Cria o ambiente virtual ----------
if not exist "!VENV!\Scripts\python.exe" (
    echo Criando ambiente virtual em !VENV! ...
    "!PYEXE!" !PYARG! -m venv "!VENV!"
    if errorlevel 1 goto :erro
)
set "VPY=!VENV!\Scripts\python.exe"

rem ---------- 4) Dependencias - so quando requirements.txt muda ----------
fc /b "%~dp0requirements.txt" "!VENV!\requirements.instalado" >nul 2>nul
if errorlevel 1 (
    echo Instalando dependencias - na primeira vez pode levar alguns minutos...
    if not exist "!VENV!\tmp" mkdir "!VENV!\tmp"
    set "TMP=!VENV!\tmp"
    set "TEMP=!VENV!\tmp"
    "!VPY!" -m pip install --disable-pip-version-check --no-cache-dir -q --upgrade pip
    "!VPY!" -m pip install --disable-pip-version-check --no-cache-dir -r "%~dp0requirements.txt"
    if errorlevel 1 goto :erro
    copy /y "%~dp0requirements.txt" "!VENV!\requirements.instalado" >nul
)

rem ---------- 5) Executa ----------
set "PYTHONPATH=%~dp0src"
set "PYTHONUTF8=1"
echo Iniciando...
"!VPY!" -m obdmon %*
set "RC=!errorlevel!"
if not "!RC!"=="0" (
    echo.
    echo O programa terminou com erro, codigo !RC!. Detalhes em logs\programa.log
    pause
)
exit /b !RC!

rem ---------------------------------------------------------------
:acha_python
set "PYEXE="
set "PYARG="
for %%V in (3.12 3.13 3.11 3.10) do (
    if not defined PYEXE (
        py -%%V -c "import sys" >nul 2>nul
        if not errorlevel 1 (
            set "PYEXE=py"
            set "PYARG=-%%V"
        )
    )
)
if defined PYEXE goto :eof
for %%P in ("%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%ProgramFiles%\Python312\python.exe") do (
    if not defined PYEXE if exist "%%~P" set "PYEXE=%%~P"
)
if defined PYEXE goto :eof
python -c "import sys; sys.exit(0 if (3,10)<=sys.version_info[:2]<(3,14) else 1)" >nul 2>nul
if not errorlevel 1 set "PYEXE=python"
goto :eof

:erro
echo.
echo Algo deu errado. Leia as mensagens acima.
pause
exit /b 1
