@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   RPA DIAN + OCR - Anyi Melisa Chaparro V.
echo ============================================
where python >nul 2>nul || (echo [ERROR] No se encontro Python. Instalalo desde python.org marcando "Add Python to PATH". & pause & exit /b 1)
where tesseract >nul 2>nul || if not exist "C:\Program Files\Tesseract-OCR\tesseract.exe" (echo [ERROR] No se encontro Tesseract. Instalalo ^(UB Mannheim^) con el idioma Spanish. & pause & exit /b 1)
if exist "C:\Program Files\Tesseract-OCR" set "PATH=%PATH%;C:\Program Files\Tesseract-OCR"

if not exist ".env" (
  echo El portal de la DIAN pide un NIT junto a cada CUFE ^(tambien es la clave del PDF^).
  set /p NIT="Escribe el NIT y presiona Enter: "
  call powershell -NoProfile -Command "(Get-Content .env.example) -replace '^DIAN_NIT=$',('DIAN_NIT=' + $env:NIT) | Set-Content .env"
)
for /f "tokens=2 delims==" %%a in ('findstr /b "DIAN_NIT=" .env') do set "NIT_ACTUAL=%%a"
echo NIT configurado: %NIT_ACTUAL%

if not exist ".venv" (
  echo [1/3] Preparando el entorno por primera vez ^(tarda unos minutos^)...
  python -m venv .venv || (pause & exit /b 1)
  call .venv\Scripts\activate
  python -m pip install --upgrade pip >nul
  pip install -r requirements.txt || (pause & exit /b 1)
  playwright install chromium || (pause & exit /b 1)
) else (
  call .venv\Scripts\activate
  pip install -q -r requirements.txt
)

rem --- Chrome normal (sin banderas de automatizacion) al que el robot se conecta ---
set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%LocalAppData%\Google\Chrome\Application\chrome.exe"
if exist "%CHROME%" goto chrome_ok
echo [AVISO] No se encontro Google Chrome: el robot abrira su propio navegador.
goto chrome_fin
:chrome_ok
echo Abriendo Google Chrome para la consulta (perfil propio del robot)...
start "" "%CHROME%" --remote-debugging-port=9222 --user-data-dir="%~dp0output\.perfil_chrome" --no-first-run --no-default-browser-check "https://catalogo-vpfe.dian.gov.co/User/SearchDocument"
set "CONECTAR_CHROME=http://127.0.0.1:9222"
timeout /t 6 >nul
:chrome_fin

echo [2/3] Validando los CUFE...
python main.py validar
echo [3/3] Ejecutando RPA + OCR. No cierres la ventana de Chrome; si el captcha no avanza, haz clic en la casilla.
python main.py todo
echo.
echo Listo. Revisa la carpeta "output": pdfs, csv, bitacora_rpa.csv y evidencias.
start "" "%~dp0output"
pause
