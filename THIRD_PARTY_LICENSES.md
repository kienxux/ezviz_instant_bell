# Third-party code and licences

The licence of this project is in `LICENSE` (MIT). The live-video part is built
on work of others, who are thanked here; their copyright notices and licence
text are kept as the licences require.

## `ezviz_hp7` by Bobsilvio (MIT)

[github.com/Bobsilvio/ezviz_hp7](https://github.com/Bobsilvio/ezviz_hp7)

Adapted into this project:

- `custom_components/ezviz_instant_bell/live_camera.py` (stream relay, live camera entity)
- `custom_components/ezviz_instant_bell/_pes.py` (MPEG-PS splitter)
- `custom_components/ezviz_instant_bell/repairs.py` (encryption code form)
- the camera-key and video-encryption logic in `stream_support.py`
- the set of entities and their behaviour, modelled on that integration

```
MIT License

Copyright (c) 2026 Silvio Vaira (Bobsilvio)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## `ezviz_hp7` fork by albrzmr (MIT)

[github.com/albrzmr/ezviz_hp7](https://github.com/albrzmr/ezviz_hp7)

The local (LAN) video protocol of the CPD7/CP7 family (ports 9010/9020, AES
control frames, ECDH key agreement, ChaCha20 media decryption) and the
motion-JPEG live view were worked out by albrzmr:

- `custom_components/ezviz_instant_bell/cpd7/`
- `custom_components/ezviz_instant_bell/mjpeg.py`
- the p2p-register and LAN-key steps in `stream_support.py`

The licence of that work is MIT. The copyright holder is albrzmr; the
permission and warranty text is the same as above.

## `pyezvizapi` by Renier Moorcroft (Apache-2.0)

[github.com/RenierM26/pyEzvizApi](https://github.com/RenierM26/pyEzvizApi)

Not copied: it is installed as a normal dependency (`pyezvizapi` from PyPI) and
keeps its own licence, <https://github.com/RenierM26/pyEzvizApi/blob/main/LICENSE.md>.
