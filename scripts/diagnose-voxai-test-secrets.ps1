& {
    $targets = @(
        [pscustomobject]@{
            Label = 'POSTGRES'
            Path = 'C:\secrets\voxai-test\postgres_password.txt'
        }
        [pscustomobject]@{
            Label = 'REDIS'
            Path = 'C:\secrets\voxai-test\redis_password.txt'
        }
    )

    foreach ($target in $targets) {
        [byte[]]$bytes = $null
        $status = 'UNAVAILABLE'
        $byteLength = 'UNAVAILABLE'
        $lowercaseHex = 'UNAVAILABLE'
        $utf8Bom = 'UNAVAILABLE'
        $hasCrlf = 'UNAVAILABLE'
        $hasLf = 'UNAVAILABLE'

        try {
            $bytes = [System.IO.File]::ReadAllBytes($target.Path)
            $byteLength = [string]$bytes.Length

            $allLowercaseHex = $bytes.Length -gt 0
            $foundCrlf = $false
            $foundLf = $false

            for ($index = 0; $index -lt $bytes.Length; $index++) {
                $value = $bytes[$index]
                $isDigit = $value -ge 0x30 -and $value -le 0x39
                $isLowerHexLetter = $value -ge 0x61 -and $value -le 0x66
                if (-not ($isDigit -or $isLowerHexLetter)) {
                    $allLowercaseHex = $false
                }

                if ($value -eq 0x0A) {
                    $foundLf = $true
                    if ($index -gt 0 -and $bytes[$index - 1] -eq 0x0D) {
                        $foundCrlf = $true
                    }
                }
            }

            $hasUtf8Bom = $bytes.Length -ge 3 -and
                $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF

            $lowercaseHex = if ($allLowercaseHex) { 'PASS' } else { 'FAIL' }
            $utf8Bom = if ($hasUtf8Bom) { 'YES' } else { 'NO' }
            $hasCrlf = if ($foundCrlf) { 'YES' } else { 'NO' }
            # LF reports any LF byte, including the LF in a CRLF sequence.
            $hasLf = if ($foundLf) { 'YES' } else { 'NO' }
            $status = if ($bytes.Length -eq 64 -and $allLowercaseHex) { 'PASS' } else { 'FAIL' }
        }
        catch {
            # Intentionally suppress exception details, which may include paths.
            $status = 'UNAVAILABLE'
            $byteLength = 'UNAVAILABLE'
            $lowercaseHex = 'UNAVAILABLE'
            $utf8Bom = 'UNAVAILABLE'
            $hasCrlf = 'UNAVAILABLE'
            $hasLf = 'UNAVAILABLE'
        }
        finally {
            if ($null -ne $bytes -and $bytes.Length -gt 0) {
                [System.Array]::Clear($bytes, 0, $bytes.Length)
            }
        }

        Write-Output ('{0} {1} byte-length={2} lowercase-hex={3} utf8-bom={4} crlf={5} lf={6}' -f `
            $target.Label, $status, $byteLength, $lowercaseHex, $utf8Bom, $hasCrlf, $hasLf)
    }
}
