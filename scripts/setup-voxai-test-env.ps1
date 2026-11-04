& {
function Set-VoxAiTestEnvironment {
    $ErrorActionPreference = 'Stop'

    # Process-local setup only. Dot-source this script in the PowerShell session
    # that will run the isolated tests; nothing is written to persistent settings.
    $secretDirectory = 'C:\secrets\voxai-test'
    $postgresPasswordPath = Join-Path $secretDirectory 'postgres_password.txt'
    $redisPasswordPath = Join-Path $secretDirectory 'redis_password.txt'
    $redisAclPath = Join-Path $secretDirectory 'redis_acl.txt'
    $environmentNames = @(
        'VOXAI_TEST_POSTGRES_PASSWORD_FILE',
        'VOXAI_TEST_REDIS_ACL_FILE',
        'VOXAI_TEST_REDIS_PASSWORD_FILE',
        'TEST_DATABASE_URL',
        'TEST_ASYNC_DATABASE_URL',
        'TEST_REDIS_URL',
        'TEST_CELERY_BROKER_URL',
        'TEST_CELERY_RESULT_BACKEND'
    )
    $originalValues = @{}
    $originalWasSet = @{}
    $assignmentsStarted = $false
    $failureStage = 'process-variable-snapshot'

    function Assert-TestPasswordBytes {
        param([Parameter(Mandatory = $true)][byte[]]$Bytes)

        if ($Bytes.Length -ne 64) {
            throw 'Invalid test-only password file.'
        }
        foreach ($byte in $Bytes) {
            $isDigit = $byte -ge 0x30 -and $byte -le 0x39
            $isLowerHex = $byte -ge 0x61 -and $byte -le 0x66
            if (-not ($isDigit -or $isLowerHex)) {
                throw 'Invalid test-only password file.'
            }
        }
    }

    function Assert-TestUrl {
        param(
            [Parameter(Mandatory = $true)][string]$Value,
            [Parameter(Mandatory = $true)][string]$Pattern,
            [Parameter(Mandatory = $true)][string]$Scheme,
            [Parameter(Mandatory = $true)][string]$Password,
            [Parameter(Mandatory = $true)][int]$Port,
            [Parameter(Mandatory = $true)][string]$Path
        )

        if ($Value -cnotmatch $Pattern -or $Password -cnotmatch '^[0-9a-f]{64}$') {
            throw 'Test endpoint validation failed.'
        }

        $uri = $null
        if (-not [Uri]::TryCreate($Value, [UriKind]::Absolute, [ref]$uri)) {
            throw 'Test endpoint validation failed.'
        }
        $expectedUserInfo = 'voxai_test:' + [Uri]::EscapeDataString($Password)
        if (
            $uri.Scheme -cne $Scheme -or
            $uri.UserInfo -cne $expectedUserInfo -or
            $uri.Host -cne '127.0.0.1' -or
            $uri.Port -ne $Port -or
            $uri.AbsolutePath -cne $Path -or
            $uri.Query -cne '' -or
            $uri.Fragment -cne '' -or
            $uri.AbsoluteUri -cne $Value
        ) {
            throw 'Test endpoint validation failed.'
        }
    }

    try {
        $failureStage = 'process-variable-snapshot'
        foreach ($name in $environmentNames) {
            $currentValue = [Environment]::GetEnvironmentVariable($name, 'Process')
            $originalWasSet[$name] = $null -ne $currentValue
            if ($originalWasSet[$name]) {
                $originalValues[$name] = [string]$currentValue
            }
        }

        $failureStage = 'secret-file-metadata'
        foreach ($path in @($postgresPasswordPath, $redisPasswordPath, $redisAclPath)) {
            $item = Get-Item -LiteralPath $path -Force
            if ($item.PSIsContainer -or $item.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) {
                throw 'Invalid test-only secret file.'
            }
        }

        # Read the two password files as raw bytes. Fixed-length lowercase hex
        # rejects BOMs, line endings, and every non-hex byte before decoding.
        $failureStage = 'password-file-read'
        $postgresPasswordBytes = [IO.File]::ReadAllBytes($postgresPasswordPath)
        $redisPasswordBytes = [IO.File]::ReadAllBytes($redisPasswordPath)

        $failureStage = 'password-file-format'
        Assert-TestPasswordBytes -Bytes $postgresPasswordBytes
        Assert-TestPasswordBytes -Bytes $redisPasswordBytes
        $postgresPassword = [Text.Encoding]::ASCII.GetString($postgresPasswordBytes)
        $redisPassword = [Text.Encoding]::ASCII.GetString($redisPasswordBytes)

        # Validate the third file as ASCII and against the exact test ACL policy
        # already enforced by conftest.py. No ACL contents are emitted.
        $failureStage = 'redis-acl-read'
        $aclBytes = [IO.File]::ReadAllBytes($redisAclPath)
        foreach ($byte in $aclBytes) {
            if ($byte -gt 0x7F) {
                throw 'Invalid test-only ACL file.'
            }
        }
        $failureStage = 'redis-acl-hash'
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            $redisHashBytes = $sha.ComputeHash($redisPasswordBytes)
        }
        finally {
            $sha.Dispose()
        }
        $redisHash = ([BitConverter]::ToString($redisHashBytes)).Replace('-', '').ToLowerInvariant()

        $failureStage = 'redis-acl-validation'
        $aclText = [Text.Encoding]::ASCII.GetString($aclBytes)
        $aclEntries = @(
            foreach ($line in ($aclText -split '\r?\n')) {
                $trimmed = $line.Trim()
                if ($trimmed.Length -gt 0 -and -not $line.TrimStart().StartsWith('#', [StringComparison]::Ordinal)) {
                    ($trimmed -split '\s+') -join ' '
                }
            }
        )
        $expectedAclEntry = "user voxai_test on #$redisHash -@all ~ratelimit:* ~lead-call-lock:* &events:* +ping +flushdb +incr +expire +set +del +publish"
        if (
            $aclEntries.Count -ne 2 -or
            $aclEntries[0] -cne 'user default off' -or
            $aclEntries[1] -cne $expectedAclEntry
        ) {
            throw 'Invalid test-only ACL file.'
        }

        $failureStage = 'url-construction'
        $postgresEncoded = [Uri]::EscapeDataString($postgresPassword)
        $redisEncoded = [Uri]::EscapeDataString($redisPassword)
        $values = @{
            TEST_DATABASE_URL = "postgresql+psycopg2://voxai_test:$postgresEncoded@127.0.0.1:55432/voxai_test"
            TEST_ASYNC_DATABASE_URL = "postgresql+asyncpg://voxai_test:$postgresEncoded@127.0.0.1:55432/voxai_test"
            TEST_REDIS_URL = "redis://voxai_test:$redisEncoded@127.0.0.1:56379/0"
            TEST_CELERY_BROKER_URL = "redis://voxai_test:$redisEncoded@127.0.0.1:56379/0"
            TEST_CELERY_RESULT_BACKEND = "redis://voxai_test:$redisEncoded@127.0.0.1:56379/0"
        }
        $secretPaths = @{
            VOXAI_TEST_POSTGRES_PASSWORD_FILE = $postgresPasswordPath
            VOXAI_TEST_REDIS_ACL_FILE = $redisAclPath
            VOXAI_TEST_REDIS_PASSWORD_FILE = $redisPasswordPath
        }
        $urlSpecs = @{
            TEST_DATABASE_URL = @{
                Pattern = '^postgresql\+psycopg2://voxai_test:[0-9a-f]{64}@127\.0\.0\.1:55432/voxai_test$'
                Scheme = 'postgresql+psycopg2'; Password = $postgresPassword; Port = 55432; Path = '/voxai_test'
            }
            TEST_ASYNC_DATABASE_URL = @{
                Pattern = '^postgresql\+asyncpg://voxai_test:[0-9a-f]{64}@127\.0\.0\.1:55432/voxai_test$'
                Scheme = 'postgresql+asyncpg'; Password = $postgresPassword; Port = 55432; Path = '/voxai_test'
            }
            TEST_REDIS_URL = @{
                Pattern = '^redis://voxai_test:[0-9a-f]{64}@127\.0\.0\.1:56379/0$'
                Scheme = 'redis'; Password = $redisPassword; Port = 56379; Path = '/0'
            }
            TEST_CELERY_BROKER_URL = @{
                Pattern = '^redis://voxai_test:[0-9a-f]{64}@127\.0\.0\.1:56379/0$'
                Scheme = 'redis'; Password = $redisPassword; Port = 56379; Path = '/0'
            }
            TEST_CELERY_RESULT_BACKEND = @{
                Pattern = '^redis://voxai_test:[0-9a-f]{64}@127\.0\.0\.1:56379/0$'
                Scheme = 'redis'; Password = $redisPassword; Port = 56379; Path = '/0'
            }
        }

        # Complete all URL validation before changing any process variable.
        $failureStage = 'url-validation'
        foreach ($name in $values.Keys) {
            $spec = $urlSpecs[$name]
            Assert-TestUrl -Value $values[$name] -Pattern $spec.Pattern `
                -Scheme $spec.Scheme -Password $spec.Password -Port $spec.Port -Path $spec.Path
        }

        $environmentValues = @{}
        foreach ($name in $secretPaths.Keys) { $environmentValues[$name] = $secretPaths[$name] }
        foreach ($name in $values.Keys) { $environmentValues[$name] = $values[$name] }

        # Only after every file and URL passes validation do assignments begin.
        $failureStage = 'process-variable-assignment'
        $assignmentsStarted = $true
        foreach ($name in $environmentNames) {
            [Environment]::SetEnvironmentVariable($name, [string]$environmentValues[$name], 'Process')
        }

        $failureStage = 'final-verification'
        foreach ($name in $values.Keys) {
            $configured = [Environment]::GetEnvironmentVariable($name, 'Process')
            $spec = $urlSpecs[$name]
            if ($configured -cne $values[$name]) {
                throw 'Test environment validation failed.'
            }
            Assert-TestUrl -Value $configured -Pattern $spec.Pattern `
                -Scheme $spec.Scheme -Password $spec.Password -Port $spec.Port -Path $spec.Path
        }
        foreach ($name in $secretPaths.Keys) {
            if ([Environment]::GetEnvironmentVariable($name, 'Process') -cne $secretPaths[$name]) {
                throw 'Test environment validation failed.'
            }
        }

        Write-Output 'Configured and validated the eight isolated test environment variables for this PowerShell process. Values were not displayed.'
    }
    catch {
        $rollbackFailed = $false
        if ($assignmentsStarted) {
            foreach ($name in $environmentNames) {
                try {
                    if ($originalWasSet[$name]) {
                        [Environment]::SetEnvironmentVariable($name, $originalValues[$name], 'Process')
                    }
                    else {
                        [Environment]::SetEnvironmentVariable($name, $null, 'Process')
                    }
                }
                catch {
                    $rollbackFailed = $true
                }
            }
        }
        if ($rollbackFailed) {
            throw "Test environment setup failed at stage '$failureStage'; rollback was incomplete."
        }
        if ($assignmentsStarted) {
            throw "Test environment setup failed at stage '$failureStage'; original process variable values were restored."
        }
        throw "Test environment setup failed at stage '$failureStage' before process variables were changed."
    }
    finally {
        $postgresPasswordBytes = $null
        $redisPasswordBytes = $null
        $aclBytes = $null
        $redisHashBytes = $null
        $postgresPassword = $null
        $redisPassword = $null
        $postgresEncoded = $null
        $redisEncoded = $null
        $redisHash = $null
        $aclText = $null
        $expectedAclEntry = $null
        $values = $null
        $urlSpecs = $null
        $secretPaths = $null
        $spec = $null
        $configured = $null
        $environmentValues = $null
        $originalValues.Clear()
        $originalWasSet.Clear()
    }
}

Set-VoxAiTestEnvironment
}
