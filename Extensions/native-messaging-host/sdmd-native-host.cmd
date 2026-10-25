@echo off
REM Super Download Manager 原生宿主启动脚本（Windows）。
REM
REM 真实脚本由 sdm.ipc.native_host.write_host_launcher() 在安装时写出来，
REM 下面这行跟的是打包好的 sdmd-native-host.exe 的实际路径。
"C:\Program Files\SuperDownloadManager\sdmd-native-host.exe" %*
