# Code review

Self-review completed with GPU execution evidence.
- Fixed an unsafe prototype assumption: CUVID can hang when source dimensions change despite fixed resize. Preselection keyframe inspection excludes these streams.
- Full-range and irregular-timing samples differ from the old renderer; explicitly excluded and byte-identical compatibility outputs verified.
- NVENC packed-RGB forces BT.601 in FFmpeg 7.1. Final version retains CPU output conversion and explicitly restores source matrix, transfer and primaries. No forced wrong header workaround.
- Renderer version, kernel hash, recipe hash and source media properties are saved locally beside the output. Existing job/profile/artifact identity and recovery journal remain unchanged.
- The worker includes bounded heartbeat reconfirmation before the next external operation; explicit rejection and actual lease expiry still stop immediately. No platform operation replay was introduced. The existing SIGTERM drain and executor behavior are preserved. Reviewed final Linux worker SHA256: 421978ad0419c514f8d48db737b3110975fb5ba01ec005a7e5b2baad326351e3; activation also recognizes the previous audited worker hash 7abc1300e57bc515a504d9295708b3d449e40c1ad9ba1dff6e6eb76a8e160b06 for safe migration.
- Activation validates clean exact releases, verified marker, node identity, drain worker hash, KillMode=mixed and infinite stop timeout. It backs up unit/env/private manifest and never edits ledger/capacity/credentials.
- Frontend changes covered by tests only; existing real-time capacity contract remains intact.
- Shared cover/contain fitting and the exact integer Lanczos shortcut produced byte-identical output to the color-qualified version on the actual GPU. Final runtime53e7016 and real generation1 prepare/publish were read back after natural drain.
