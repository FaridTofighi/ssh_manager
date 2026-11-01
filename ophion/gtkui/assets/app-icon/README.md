# Application icon (`ophion`)

- **Source:** `Ophion_App_Icon_Package.zip` → `linux/hicolor/<size>/apps/ophion.png`,
  copied unchanged (the approved package).
- **Colourway:** dark-multicolor (orange snake, white ring, teal band on a dark
  navy square; opaque, no rounded corners).
- **Sizes:** 16, 22, 24, 32, 48, 64, 96, 128, 256, 512 px, in the hicolor layout
  so this folder can be added to the GTK icon theme search path
  (`gtkui/widgets/icons.py: install_app_icon`) and copied to
  `share/icons/hicolor` by the packaging.
- **Raster only, no SVG.** Do not resample these files. For sizes above 512 px,
  or a sharper small size, ask for a vector redraw of the artwork.
