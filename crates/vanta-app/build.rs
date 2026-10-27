fn main() {
    let config = slint_build::CompilerConfiguration::new().with_style("fluent".into());
    slint_build::compile_with_config("ui/app.slint", config).expect("Slint UI failed to compile");
    println!("cargo:rerun-if-env-changed=VANTA_MSA_CLIENT_ID");

    // Icon and version info for the Windows executable.
    #[cfg(windows)]
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") {
        let mut res = winresource::WindowsResource::new();
        res.set_icon("../../packaging/icons/vanta.ico")
            .set("ProductName", "Vanta")
            .set("FileDescription", "Vanta")
            .set("CompanyName", "Inpriv")
            .set("LegalCopyright", "Inpriv. GPL-3.0-or-later")
            .set("OriginalFilename", "vanta.exe");
        res.compile().expect("failed to embed Windows resources");
    }
}
