# RPA Worker 一键安装（Windows PowerShell）
# 用法：右键以 PowerShell 运行，或 powershell -ExecutionPolicy Bypass -File setup.ps1
$ErrorActionPreference = "Stop"

Write-Host "==> 创建 Python 虚拟环境..." -ForegroundColor Cyan
python -m venv .venv
.\.venv\Scripts\Activate.ps1

Write-Host "==> 安装依赖..." -ForegroundColor Cyan
python -m pip install --upgrade pip
pip install -r requirements.txt

Write-Host "==> 安装 Chromium 浏览器..." -ForegroundColor Cyan
playwright install chromium

if (!(Test-Path .env.local)) {
    Copy-Item .env.example .env.local
    $key = python -c "import secrets;print(secrets.token_hex(16))"
    (Get-Content .env.local) -replace "^RPA_API_KEY=$", "RPA_API_KEY=$key" | Set-Content .env.local -Encoding utf8
    Write-Host "==> 已生成 .env.local，RPA_API_KEY 随机生成：$key" -ForegroundColor Green
    Write-Host "    请把同一个 key 填到后端 .env 的 RPA_API_KEY，并修改 .env.local 的 BACKEND_URL / ACCOUNT" -ForegroundColor Yellow
} else {
    Write-Host "==> .env.local 已存在，跳过生成" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "安装完成！接下来的步骤：" -ForegroundColor Green
Write-Host "  1. 编辑 .env.local（BACKEND_URL / ACCOUNT / PLATFORM）"
Write-Host "  2. python login.py        # 首次登录引导（扫码）"
Write-Host "  3. python doctor.py       # 自检全绿后"
Write-Host "  4. python douyin_feige_worker.py   # 或 xhs_ark_worker.py"
