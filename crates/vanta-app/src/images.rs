//! Remote image loading: Modrinth icons and player heads.
//!
//! Decoding happens on worker threads; only the finished pixel buffer crosses
//! to the UI thread. Icons are downscaled to keep memory use low.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Mutex;

use sha2::{Digest, Sha256};
use slint::{Rgba8Pixel, SharedPixelBuffer};

pub type Pixels = SharedPixelBuffer<Rgba8Pixel>;

const ICON_SIZE: u32 = 96;
const MEMORY_LIMIT: usize = 400;

pub struct ImageCache {
    dir: PathBuf,
    client: reqwest::Client,
    memory: Mutex<HashMap<String, Pixels>>,
}

fn to_pixels(img: image::RgbaImage) -> Pixels {
    SharedPixelBuffer::clone_from_slice(img.as_raw(), img.width(), img.height())
}

impl ImageCache {
    pub fn new(dir: PathBuf, client: reqwest::Client) -> Self {
        Self {
            dir,
            client,
            memory: Mutex::new(HashMap::new()),
        }
    }

    fn disk_path(&self, url: &str) -> PathBuf {
        self.dir.join(hex::encode(&Sha256::digest(url.as_bytes())[..12]))
    }

    async fn bytes(&self, url: &str) -> Option<Vec<u8>> {
        let path = self.disk_path(url);
        if let Ok(b) = tokio::fs::read(&path).await {
            return Some(b);
        }
        let res = self.client.get(url).send().await.ok()?;
        if !res.status().is_success() {
            return None;
        }
        let bytes = res.bytes().await.ok()?.to_vec();
        let _ = tokio::fs::create_dir_all(&self.dir).await;
        let _ = tokio::fs::write(&path, &bytes).await;
        Some(bytes)
    }

    /// A square icon, downscaled to at most 96 px.
    pub async fn icon(&self, url: &str) -> Option<Pixels> {
        if let Some(p) = self.memory.lock().unwrap().get(url) {
            return Some(p.clone());
        }
        let bytes = self.bytes(url).await?;
        let pixels = tokio::task::spawn_blocking(move || {
            let img = image::load_from_memory(&bytes).ok()?;
            let img = if img.width() > ICON_SIZE || img.height() > ICON_SIZE {
                img.thumbnail(ICON_SIZE, ICON_SIZE)
            } else {
                img
            };
            Some(to_pixels(img.to_rgba8()))
        })
        .await
        .ok()??;
        let mut mem = self.memory.lock().unwrap();
        if mem.len() > MEMORY_LIMIT {
            mem.clear();
        }
        mem.insert(url.to_owned(), pixels.clone());
        Some(pixels)
    }

    /// 8×8 face (with hat layer) cut from a Minecraft skin.
    pub async fn head(&self, skin_url: &str) -> Option<Pixels> {
        let key = format!("head:{skin_url}");
        if let Some(p) = self.memory.lock().unwrap().get(&key) {
            return Some(p.clone());
        }
        let bytes = self.bytes(skin_url).await?;
        let pixels = tokio::task::spawn_blocking(move || head_from_skin(&bytes))
            .await
            .ok()??;
        self.memory.lock().unwrap().insert(key, pixels.clone());
        Some(pixels)
    }
}

pub fn head_from_skin(bytes: &[u8]) -> Option<Pixels> {
    let skin = image::load_from_memory(bytes).ok()?.to_rgba8();
    if skin.width() < 64 || skin.height() < 32 {
        return None;
    }
    let mut face = image::imageops::crop_imm(&skin, 8, 8, 8, 8).to_image();
    let hat = image::imageops::crop_imm(&skin, 40, 8, 8, 8).to_image();
    image::imageops::overlay(&mut face, &hat, 0, 0);
    Some(to_pixels(face))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cuts_the_face_out_of_a_skin() {
        let mut skin = image::RgbaImage::new(64, 64);
        for x in 8..16 {
            for y in 8..16 {
                skin.put_pixel(x, y, image::Rgba([200, 0, 0, 255]));
            }
        }
        // Hat pixel overrides the face at (0, 0).
        skin.put_pixel(40, 8, image::Rgba([0, 0, 255, 255]));
        let mut png = Vec::new();
        skin.write_to(&mut std::io::Cursor::new(&mut png), image::ImageFormat::Png)
            .unwrap();
        let head = head_from_skin(&png).unwrap();
        assert_eq!((head.width(), head.height()), (8, 8));
        let px = head.as_slice();
        assert_eq!((px[0].r, px[0].b), (0, 255));
        assert_eq!((px[9].r, px[9].a), (200, 255));
    }
}
