#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
.SYNOPSIS
  PAPER 무인 자동 운영 예약 작업 등록 (T-025, CP-029, 사용자 결정 A-078). 사용자가 관리자 PowerShell에서 한 번 실행한다.

.DESCRIPTION
  현재 로그인 사용자 계정으로 작업 2개를 등록한다 (작업 이름 접두어 TradingSystem-PAPER-).
    1) TradingSystem-PAPER-ClockSync : 평일 06:40  w32tm /resync  (시계 동기화, 관리자 권한이 필요해 이 작업만 "최고 권한")
    2) TradingSystem-PAPER-Autopilot : 평일 06:45  .venv\Scripts\pythonw.exe scripts\autopilot.py --mode PAPER  (일반 권한, 창 없음)
  - PAPER 전용. LIVE 작업은 만들지 않는다. autopilot.py는 --mode PAPER 외에는 거부한다.
  - 비밀값(앱 키·토큰·계좌번호)을 명령줄·작업 정의에 넣지 않는다. 자동 운영이 실행 때 .env.paper를 읽는다.
  - 로그온한 동안만 실행(Interactive) — 암호를 저장하지 않는다. PC는 켜 두고 로그인 상태를 유지한다(화면 잠금은 괜찮음).
  - 놓친 실행(PC가 꺼져 있던 경우)은 나중에 따로 실행하지 않는다(StartWhenAvailable 꺼짐).
  - 휴장일 판단은 autopilot.py가 config/calendar로 한다(휴장일이면 텔레그램 1줄 후 종료).
  - 같은 이름의 작업이 이미 있으면 아무것도 바꾸지 않고 멈춘다(먼저 scripts\schedule-unregister.ps1).
  - 에이전트는 이 스크립트를 실행하지 않는다. 등록·해제는 사용자가 한다 (CLAUDE.md §4 자동 실행 등록).

.EXAMPLE
  관리자 권한 PowerShell에서:
    cd C:\AI\trading_system\trading_system
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\schedule-register.ps1
#>
[CmdletBinding()]
param(
    [string]$Root = '',
    [switch]$DryRun     # 경로·모드 줄·계정만 확인하고 등록하지 않는다
)

$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1에서 -File로 실행하면 param() 기본값 계산 시점에 $PSScriptRoot가 비어 있다(2026-10-09 실측).
# 그래서 기본값이 아니라 본문에서 계산한다.
if (-not $Root) {
    $here = $PSScriptRoot
    if (-not $here) { $here = Split-Path -Parent $MyInvocation.MyCommand.Path }
    $Root = Split-Path -Parent $here
}
$Prefix = 'TradingSystem-PAPER-'
$ClockTask = $Prefix + 'ClockSync'
$AutoTask = $Prefix + 'Autopilot'
$Days = @('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday')

$Root = (Resolve-Path -LiteralPath $Root).Path
$Pythonw = Join-Path $Root '.venv\Scripts\pythonw.exe'
$Script = Join-Path $Root 'scripts\autopilot.py'
$W32tm = Join-Path $env:SystemRoot 'System32\w32tm.exe'
foreach ($path in @($Pythonw, $Script, $W32tm)) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "파일 없음: $path"
    }
}
# 예약 실행에는 환경 변수가 없어 자동 운영이 루트 .env의 TRADE_MODE 한 줄로 모드를 확인한다(autopilot.py mode_refusal).
# .env.paper의 TRADE_MODE 줄은 따로 있는 교차 확인용이다. 값은 출력하지 않고 TRADE_MODE 줄만 본다.
$ModeFile = Join-Path $Root '.env'
$modeLine = $null
if (Test-Path -LiteralPath $ModeFile) {
    $modeLine = Get-Content -LiteralPath $ModeFile | Where-Object { $_ -match '^\s*TRADE_MODE\s*=' } | Select-Object -First 1
}
if (-not $modeLine -or ($modeLine -replace '\s', '') -ne 'TRADE_MODE=PAPER') {
    throw "루트 .env에 'TRADE_MODE=PAPER' 한 줄이 없습니다 ($ModeFile). 예약 실행은 이 줄로 모드를 확인하므로 없으면 매일 시작을 거부합니다. 사용 설명서 §2의 .env 만들기 명령을 먼저 실행하세요."
}
foreach ($name in @($ClockTask, $AutoTask)) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        throw "이미 등록된 작업: $name — 바꾸려면 먼저 scripts\schedule-unregister.ps1 실행"
    }
}
$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
if ($DryRun) {
    Write-Host '확인만 (등록 안 함)'
    Write-Host ('  저장소: {0}' -f $Root)
    Write-Host ('  실행 파일: {0}' -f $Pythonw)
    Write-Host ('  자동 운영: {0}' -f $Script)
    Write-Host '  모드 줄: TRADE_MODE=PAPER 확인'
    Write-Host ('  등록될 계정: {0}  (평소 로그인 계정과 같아야 함)' -f $User)
    exit 0
}

# 1) 시계 동기화 (관리자 권한 필요 — 이 작업만 최고 권한). 실패해도 시작 점검이 시계 오차 1초 초과면 시작을 거부한다.
$clockAction = New-ScheduledTaskAction -Execute $W32tm -Argument '/resync'
$clockTrigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek $Days -At '06:40'
$clockPrincipal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Highest
$clockSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $ClockTask -Action $clockAction -Trigger $clockTrigger -Principal $clockPrincipal `
    -Settings $clockSettings -Description 'PAPER 자동 운영 전 시계 동기화 (CP-029). 해제: scripts\schedule-unregister.ps1' | Out-Null

# 2) 자동 운영 (일반 권한, 창 없음). 하루를 맡는다: 시작 점검 → run → K8 자동 재개 → 15:45 종료 절차 → 일일 요약.
$autoArgs = '"{0}" --mode PAPER' -f $Script
$autoAction = New-ScheduledTaskAction -Execute $Pythonw -Argument $autoArgs -WorkingDirectory $Root
$autoTrigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek $Days -At '06:45'
$autoPrincipal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$autoSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 11) `
    -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $AutoTask -Action $autoAction -Trigger $autoTrigger -Principal $autoPrincipal `
    -Settings $autoSettings -Description 'PAPER 무인 자동 운영 (CP-029, PAPER 전용). 해제: scripts\schedule-unregister.ps1' | Out-Null

Write-Host '등록 완료 (사용자:' $User ')'
Get-ScheduledTask -TaskName ($Prefix + '*') | Format-Table TaskName, State -AutoSize
Write-Host '다음 실행 시각:'
foreach ($name in @($ClockTask, $AutoTask)) {
    $info = Get-ScheduledTaskInfo -TaskName $name
    Write-Host ('  {0}  {1}' -f $name, $info.NextRunTime)
}
