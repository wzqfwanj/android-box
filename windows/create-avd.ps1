# 创建 AVD wkeeli（480x854 / Android 11 x86_64 / 3GB RAM）
$ErrorActionPreference = "Stop"
$avdHome = Join-Path $env:USERPROFILE ".android\avd"
$avdDir = Join-Path $avdHome "wkeeli.avd"
New-Item -ItemType Directory -Force -Path $avdHome, $avdDir | Out-Null

$config = @"
AvdId=wkeeli
PlayStore.enabled=false
abi.type=x86_64
avd.ini.encoding=UTF-8
avd.ini.displayname=wkeeli
disk.dataPartition.size=8G
fastboot.forceColdBoot=yes
hw.accelerometer=yes
hw.audioInput=none
hw.audioOutput=none
hw.battery=yes
hw.camera.back=none
hw.camera.front=none
hw.cpu.arch=x86_64
hw.cpu.ncore=4
hw.dPad=no
hw.device.manufacturer=Google
hw.device.name=pixel_4
hw.gps=yes
hw.gpu.enabled=yes
hw.gpu.mode=angle_indirect
hw.keyboard=yes
hw.lcd.density=240
hw.lcd.height=854
hw.lcd.width=480
hw.mainKeys=no
hw.ramSize=3072
hw.sdCard=yes
hw.sensors.orientation=yes
hw.sensors.proximity=yes
hw.trackBall=no
image.sysdir.1=system-images/android-30/default/x86_64/
runtime.network.latency=none
runtime.network.speed=full
showDeviceFrame=no
tag.display=Default Android System Image
tag.id=default
target=android-30
vm.heapSize=512
"@
Set-Content -Path (Join-Path $avdDir "config.ini") -Value $config -Encoding UTF8

$ini = @"
avd.ini.encoding=UTF-8
path=$(Join-Path $avdDir '')
path.rel=avd\wkeeli.avd
target=android-30
"@
Set-Content -Path (Join-Path $avdHome "wkeeli.ini") -Value $ini -Encoding UTF8

Write-Output "AVD created: $avdDir"
Get-ChildItem $avdHome | Select-Object Name | Format-Table -AutoSize
