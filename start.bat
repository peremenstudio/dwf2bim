@echo off
rem Opens the dwf2bim page. The first run installs what it needs.
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install Python 3.11 or newer from python.org, and tick "Add to PATH".
    pause
    exit /b 1
)
python -c "import ifcopenshell, shapely, ezdxf, PIL, numpy, playwright" >nul 2>nul
if errorlevel 1 (
    echo Installing what dwf2bim needs. This happens once.
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo The installation failed. See the messages above.
        pause
        exit /b 1
    )
)
set PYTHONPATH=src
python -m dwf2bim ui
pause
