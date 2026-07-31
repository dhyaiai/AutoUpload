@echo off
chcp 65001 >nul
echo ========================================
echo 作业自动上传工具 - 打包脚本
echo ========================================
echo.

echo [1/4] 检查Python环境...
python --version >nul 2>&1
if errorlevel 1 (
    echo 错误: 未检测到Python环境!
    echo 请先安装 Python 3.8+
    pause
    exit /b 1
)
echo Python环境正常

echo.
echo [2/4] 检查并安装依赖包...
pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo 正在安装 PyInstaller...
    pip install pyinstaller
)

echo 正在检查其他依赖...
pip install -r requirements.txt

echo.
echo [3/4] 开始打包(这可能需要几分钟)...
echo 使用 build.spec 配置文件进行打包...
pyinstaller build.spec --clean

echo.
echo [4/4] 清理临时文件...
if exist build rmdir /s /q build
if exist __pycache__ rmdir /s /q __pycache__

echo.
echo ========================================
echo ✓ 打包完成!
echo ========================================
echo.
echo exe文件位置: dist\HomeworkAutoUpload.exe
echo.
echo 重要提示:
echo 1. 首次运行前,请确保 config.json 已正确配置
echo 2. 需要将 ChromeDriver 放在系统PATH中或使用Chrome内置驱动
echo 3. 建议将exe文件和config.json放在同一目录
echo.
pause
