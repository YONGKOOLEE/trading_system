#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
.SYNOPSIS
  PAPER 무인 자동 운영 예약 작업 해제 (T-025, CP-029). 사용자가 관리자 PowerShell에서 실행한다.

.DESCRIPTION
  schedule-register.ps1이 만든 작업 2개(TradingSystem-PAPER-ClockSync, TradingSystem-PAPER-Autopilot)를 지운다.
  - 이 이름의 작업만 지운다. 다른 작업은 건드리지 않는다. 없으면 "없음"만 알린다.
  - 지금 돌고 있는 오늘의 자동 운영·run은 멈추지 않는다. 오늘 run을 끝내려면 정지 요청 파일을 만든다:
      New-Item -ItemType File -Force .\var\paper\STOP
    (run이 Ctrl+C와 같은 순서로 종료하고, 자동 운영은 그것을 보고 paper-review·백업·요약 뒤 끝난다)
  - 해제 뒤에는 docs/strategies/paper0-verify/pv-procedure.md §3의 수동 절차로 운영한다.
  - 에이전트는 이 스크립트를 실행하지 않는다.

.EXAMPLE
  관리자 권한 PowerShell에서:
    cd C:\AI\trading_system\trading_system
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\schedule-unregister.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Prefix = 'TradingSystem-PAPER-'
foreach ($name in @(($Prefix + 'ClockSync'), ($Prefix + 'Autopilot'))) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "해제: $name"
    }
    else {
        Write-Host "없음: $name"
    }
}
$left = @(Get-ScheduledTask -TaskName ($Prefix + '*') -ErrorAction SilentlyContinue)
Write-Host ('남은 TradingSystem-PAPER- 작업: {0}개' -f $left.Count)
