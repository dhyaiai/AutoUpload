# 作业自动上传工具 - 简化版打包脚本
# 使用直接命令而不是spec文件

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Homework Auto Upload Tool - Simple Build" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Check Python
Write-Host "[1/3] Checking Python..." -ForegroundColor Yellow
python --version
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Python not found!" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host ""

# Install dependencies
Write-Host "[2/3] Installing dependencies..." -ForegroundColor Yellow
pip install pyinstaller selenium watchdog python-docx pdfplumber requests
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to install dependencies!" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host ""

# Build exe
Write-Host "[3/3] Building EXE..." -ForegroundColor Yellow
Write-Host "This may take 5-10 minutes, please wait..." -ForegroundColor Cyan
Write-Host ""

pyinstaller --onefile --windowed --name "HomeworkAutoUpload" `
    --add-data "config.json;." `
    --hidden-import=db_manager `
    --hidden-import=config_manager `
    --hidden-import=file_monitor `
    --hidden-import=upload_processor `
    --hidden-import=browser_automation `
    --hidden-import=gui_manager `
    --hidden-import=info_extractor `
    --hidden-import=subject_classifier `
    main.py 2>&1 | ForEach-Object { Write-Host $_ }

Write-Host ""

# Check result
Start-Sleep -Seconds 2
if (Test-Path "dist\HomeworkAutoUpload.exe") {
    $fileSize = (Get-Item "dist\HomeworkAutoUpload.exe").Length / 1MB
    Write-Host "========================================" -ForegroundColor Green
    Write-Host "OK Build completed successfully!" -ForegroundColor Green
    Write-Host "========================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "EXE file location: dist\HomeworkAutoUpload.exe" -ForegroundColor Cyan
    Write-Host "File size: $([math]::Round($fileSize, 2)) MB" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Important notes:" -ForegroundColor Yellow
    Write-Host "1. Before first run, make sure config.json is properly configured" -ForegroundColor White
    Write-Host "2. ChromeDriver needs to be in system PATH or use Chrome built-in driver" -ForegroundColor White
    Write-Host "3. Recommend placing exe file and config.json in the same directory" -ForegroundColor White
} else {
    Write-Host "========================================" -ForegroundColor Red
    Write-Host "ERROR: Build failed!" -ForegroundColor Red
    Write-Host "========================================" -ForegroundColor Red
    Write-Host ""
    Write-Host "Please check the error messages above" -ForegroundColor Red
}

Write-Host ""
Read-Host "Press Enter to exit"
