@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   RPA DIAN + OCR - Anyi Melisa Chaparro V.
echo ============================================
where python >nul 2>nul || (echo [ERROR] No se encontro Python. Instalalo desde python.org marcando "Add Python to PATH". & pause & exit /b 1)
where tesseract >nul 2>nul || if not exist "C:\Program Files\Tesseract-OCR\tesseract.exe" (echo [ERROR] No se encontro Tesseract. Instalalo ^(UB Mannheim^) con el idioma Spanish. & pause & exit /b 1)
if exist "C:\Program Files\Tesseract-OCR" set "PATH=%PATH%;C:\Program Files\Tesseract-OCR"

if not exist ".venv" (
  echo [1/3] Preparando el entorno por primera vez ^(tarda unos minutos^)...
  python -m venv .venv || (pause & exit /b 1)
  call .venv\Scripts\activate
  python -m pip install --upgrade pip >nul
  pip install -r requirements.txt || (pause & exit /b 1)
  playwright install chromium || (pause & exit /b 1)
) else (
  call .venv\Scripts\activate
)

echo [2/3] Validando los CUFE...
python main.py validar
echo [3/3] Ejecutando RPA + OCR. No cierres la ventana de Chrome; si el captcha no avanza, haz clic en la casilla.
python main.py todo
echo.
echo Listo. Revisa la carpeta "output": pdfs, csv, bitacora_rpa.csv y evidencias.
start "" "%~dp0output"
pause
