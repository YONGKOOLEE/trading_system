#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
.SYNOPSIS
  M2 PAPER 무인 운영 예약 작업 해제 (CP-037 T-055). 사용자가 관리자 PowerShell에서 실행한다.

.DESCRIPTION
  schedule-register-m2.ps1이 만든 작업 2개(TradingSystem-PAPER-M2-Supervisor, TradingSystem-PAPER-M2-ClockSync)를 지운다.
  - 이 두 이름만 지운다. M1 작업(TradingSystem-PAPER-ClockSync·TradingSystem-PAPER-Autopilot)과 다른 작업은 건드리지 않는다.
    없으면 "없음"만 알린다.
  - 지금 돌고 있는 감독·run은 멈추지 않는다. 멈추려면(run은 정지 요청 파일로 정상 종료, 강제 종료 없음):
      cd C:\AI\trading_system\m2-binance
      C:\AI\trading_system\trading_system\.venv\Scripts\python.exe scripts\m2-supervisor.py --mode PAPER --market M2 --stop
  - 에이전트는 이 스크립트를 실행하지 않는다.

.EXAMPLE
  관리자 권한 PowerShell에서:
    cd C:\AI\trading_system\m2-binance
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\schedule-unregister-m2.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Prefix = 'TradingSystem-PAPER-M2-'
foreach ($name in @(($Prefix + 'Supervisor'), ($Prefix + 'ClockSync'))) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "해제: $name"
    }
    else {
        Write-Host "없음: $name"
    }
}
$left = @(Get-ScheduledTask -TaskName ($Prefix + '*') -ErrorAction SilentlyContinue)
Write-Host ('남은 TradingSystem-PAPER-M2- 작업: {0}개' -f $left.Count)
