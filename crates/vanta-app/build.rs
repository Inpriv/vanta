fn main() {
    let config = slint_build::CompilerConfiguration::new().with_style("fluent".into());
    slint_build::compile_with_config("ui/app.slint", config).expect("Slint UI failed to compile");
    println!("cargo:rerun-if-env-changed=VANTA_MSA_CLIENT_ID");
}
