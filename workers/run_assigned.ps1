# 绑定变更或进程退出后隔几秒再拉起。Ctrl+C 结束循环。
Set-Location $PSScriptRoot
while ($true) {
    python run_assigned.py
    if ($LASTEXITCODE -eq 130) { break }
    Start-Sleep -Seconds 5
}
