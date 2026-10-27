//! Small platform helpers.

/// Total physical memory in MiB.
pub fn total_memory_mb() -> Option<u32> {
    #[cfg(target_os = "linux")]
    {
        let text = std::fs::read_to_string("/proc/meminfo").ok()?;
        let kb: u64 = text
            .lines()
            .find_map(|l| l.strip_prefix("MemTotal:"))?
            .trim()
            .trim_end_matches("kB")
            .trim()
            .parse()
            .ok()?;
        Some((kb / 1024) as u32)
    }
    #[cfg(windows)]
    {
        use windows_sys::Win32::System::SystemInformation::{GlobalMemoryStatusEx, MEMORYSTATUSEX};
        let mut status: MEMORYSTATUSEX = unsafe { std::mem::zeroed() };
        status.dwLength = std::mem::size_of::<MEMORYSTATUSEX>() as u32;
        // SAFETY: `status` is a properly sized, initialised MEMORYSTATUSEX.
        let ok = unsafe { GlobalMemoryStatusEx(&mut status) };
        (ok != 0).then(|| (status.ullTotalPhys / (1024 * 1024)) as u32)
    }
    #[cfg(not(any(target_os = "linux", windows)))]
    {
        None
    }
}
