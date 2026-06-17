# 作业自动上传工具 - PowerShell 打包脚本
# 使用方法: .\build.ps1

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Homework Auto Upload Tool - Build Script" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# [1/4] Check Python environment
Write-Host "[1/4] Checking Python environment..." -ForegroundColor Yellow
try {
    $pythonVersion = python --version 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "OK Python environment normal: $pythonVersion" -ForegroundColor Green
    } else {
        throw "Python not found"
    }
} catch {
    Write-Host "ERROR: Python not detected!" -ForegroundColor Red
    Write-Host "Please install Python 3.8+ first" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host ""

# [2/4] Check and install dependencies
Write-Host "[2/4] Checking and installing dependencies..." -ForegroundColor Yellow

# Check PyInstaller
$pyinstallerCheck = pip show pyinstaller 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing PyInstaller..." -ForegroundColor Cyan
    pip install pyinstaller
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: PyInstaller installation failed!" -ForegroundColor Red
        Read-Host "Press Enter to exit"
        exit 1
    }
    Write-Host "OK PyInstaller installed successfully" -ForegroundColor Green
} else {
    Write-Host "OK PyInstaller already installed" -ForegroundColor Green
}

Write-Host "Checking other dependencies..." -ForegroundColor Cyan
pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Dependencies installation failed!" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}
Write-Host "OK Dependencies installation completed" -ForegroundColor Green

Write-Host ""

# [3/4] Start building
Write-Host "[3/4] Starting build (this may take a few minutes)..." -ForegroundColor Yellow
Write-Host "Using build.spec configuration file..." -ForegroundColor Cyan
Write-Host ""

# Run pyinstaller and capture all output
$pyinstallerOutput = pyinstaller build.spec --clean 2>&1
$pyinstallerOutput | ForEach-Object { Write-Host $_ }

# Check if build was successful by looking for exe file
Start-Sleep -Seconds 2
if (Test-Path "dist\HomeworkAutoUpload.exe") {
    Write-Host "" 
    Write-Host "OK Build completed successfully!" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "ERROR: Build failed or EXE not generated!" -ForegroundColor Red
    Write-Host "Please check the error messages above" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host ""

# [4/4] Clean up temporary files
Write-Host "[4/4] Cleaning up temporary files..." -ForegroundColor Yellow

if (Test-Path "build") {
    Remove-Item -Recurse -Force "build"
}

# Don't delete build.spec, it's needed for future builds
# if (Test-Path "*.spec") {
#     Remove-Item -Force *.spec
# }

if (Test-Path "__pycache__") {
    Remove-Item -Recurse -Force "__pycache__"
}

Write-Host "OK Temporary files cleaned up" -ForegroundColor Green

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "OK Build completed!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""

# Check if exe was generated
if (Test-Path "dist\HomeworkAutoUpload.exe") {
    $fileSize = (Get-Item "dist\HomeworkAutoUpload.exe").Length / 1MB
    Write-Host "EXE file location: dist\HomeworkAutoUpload.exe" -ForegroundColor Cyan
    Write-Host "File size: $([math]::Round($fileSize, 2)) MB" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Important notes:" -ForegroundColor Yellow
    Write-Host "1. Before first run, make sure config.json is properly configured" -ForegroundColor White
    Write-Host "2. ChromeDriver needs to be in system PATH or use Chrome built-in driver" -ForegroundColor White
    Write-Host "3. Recommend placing exe file and config.json in the same directory" -ForegroundColor White
    Write-Host ""
} else {
    Write-Host "WARNING: Generated EXE file not found!" -ForegroundColor Red
    Write-Host "Please check the error messages above" -ForegroundColor Red
    Write-Host ""
}

Read-Host "Press Enter to exit"
