@echo off
set "HOST=127.0.0.1"
set "PORT=8187"

set "OBJECT_STORAGE_ENABLED=true"
set "OBJECT_STORAGE_TYPE=cos"
set "OBJECT_STORAGE_PROVIDER=cos"
set "OBJECT_STORAGE_BASE_URL=cos.ap-singapore.myqcloud.com"
set "OBJECT_STORAGE_CHANNEL=antblack-1259737057"
set "OBJECT_STORAGE_PUBLIC_BASE_URL=https://%OBJECT_STORAGE_CHANNEL%.cos.ap-singapore.myqcloud.com"
set "OBJECT_STORAGE_API_KEY=YOUR_COS_SECRET_ID"
set "OBJECT_STORAGE_SECRET_KEY=YOUR_COS_SECRET_KEY"
set "OBJECT_STORAGE_REGION=ap-singapore"

..\..\..\python_embeded\python.exe fast_api.py ^
  --host "%HOST%" ^
  --port %PORT% ^
  --object-storage-enabled "%OBJECT_STORAGE_ENABLED%" ^
  --object-storage-type "%OBJECT_STORAGE_TYPE%" ^
  --object-storage-provider "%OBJECT_STORAGE_PROVIDER%" ^
  --object-storage-base-url "%OBJECT_STORAGE_BASE_URL%" ^
  --object-storage-public-base-url "%OBJECT_STORAGE_PUBLIC_BASE_URL%" ^
  --object-storage-api-key "%OBJECT_STORAGE_API_KEY%" ^
  --object-storage-secret-key "%OBJECT_STORAGE_SECRET_KEY%" ^
  --object-storage-channel "%OBJECT_STORAGE_CHANNEL%" ^
  --object-storage-region "%OBJECT_STORAGE_REGION%"
pause
