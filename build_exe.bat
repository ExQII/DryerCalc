@echo off
chcp 65001 >nul
rem Сборка DryerCalc на Windows (нужен Python 3.10-3.12 64 бит в PATH).
rem Результат: dist\DryerCalc.exe (переносной), dist\DryerCalc\ (папка) и, если установлен NSIS, dist\DryerCalc_Setup.exe
python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements-build.txt
python tests\test_model.py
if errorlevel 1 (
    echo Тесты ядра не прошли, сборка остановлена.
    exit /b 1
)
python tests\test_features.py
if errorlevel 1 (
    echo Тесты v0.4 не прошли, сборка остановлена.
    exit /b 1
)
pyinstaller --noconfirm --clean DryerCalc.spec
if errorlevel 1 (
    echo Сборка не удалась.
    exit /b 1
)
pyinstaller --noconfirm DryerCalc_dir.spec
if errorlevel 1 (
    echo Сборка папки не удалась.
    exit /b 1
)
rem самопроверка собранного файла (окно без консоли, поэтому результат пишется в файл)
start /wait "" dist\DryerCalc.exe --selftest dist\selftest.txt
type dist\selftest.txt
python tools\make_manual.py dist
where makensis >nul 2>nul
if errorlevel 1 (
    echo NSIS не найден: установщик не собран ^(https://nsis.sourceforge.io, затем makensis installer.nsi^).
) else (
    makensis installer.nsi
)
echo.
echo Готово: dist\DryerCalc.exe
pause
