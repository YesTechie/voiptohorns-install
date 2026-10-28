# VoIP to Horns

Page your UniFi AI Horns and AI Speakers from UniFi Talk phones. Dial a zone extension and make live announcements across your warehouse, dock or office.

VoIP to Horns runs on a Raspberry Pi on your network. You create your own paging zones in its dashboard and add your AI Horns and AI Speakers to them.

## Features

Requires UniFi Talk and UniFi Protect with AI Horns or AI Speakers.

- **Page from any Talk phone.** Each zone has its own UniFi Talk extension. Dial it and your voice plays live on that zone's AI Horns and AI Speakers.
- **Zones in the dashboard.** Create a zone and the Talk user and phone are set up for you. Rename it, change its extension, pick its horns and speakers or delete it. The dashboard only changes zones it created.
- **Per-speaker delay.** Set a delay on each horn or speaker so everything in a zone stays in sync.
- **Hold to talk in the browser.** Talk through your computer or phone mic by holding the button or the Space bar. The installer sets up HTTPS and a certificate you can install on a Mac or iPhone.
- **Text-to-speech.** Type up to 120 characters and they're read out on the zone's horns and speakers.
- **Test announcement.** Play a stored clip to a zone with one click to check the horns and speakers without a mic.
- **Automatic speaker setup.** Speakers added to a zone are switched into talk mode for you.
- **Status and logs.** See the console connection, zones, horns and speakers, and bridge status, follow a live log, and restart the bridge with one button.
- **Secure dashboard.** It's protected by a login, and you connect your UniFi console from inside the dashboard.
- **Light, dark and system themes.**
- **One-command install, upgrade and uninstall.** Upgrades keep your settings. It runs on a Raspberry Pi 4 or 5 with Raspberry Pi OS based on Debian 12 or 13.

## Requirements

- Raspberry Pi 4 or 5 with 64-bit Raspberry Pi OS (Debian 12 or 13)
- UniFi Protect, with AI Horns or AI Speakers
- UniFi Talk

## Install

Run this on the Pi:

    curl -fsSL https://labs.yestechie.com/install/voiptohorns.sh | bash

The installer asks for your UniFi console address and a LOCAL UniFi OS account on the console.

## More

Details, a demo and setup notes are on the [VoIP to Horns page](https://labs.yestechie.com/integrations/voiptohorns.html).

## About YesTechie Labs

You bring the task. We build the integration.

[YesTechie](https://yestechie.com) engineers build custom UniFi integrations for businesses. We're the same team behind our UniFi projects and installations. Each integration on [YesTechie Labs](https://labs.yestechie.com) started as a real client project, and we publish a personal-use version there to show what we can build.

For business use, or for an integration built for your site, [contact us](https://yestechie.com). Join us on Reddit at [r/YesTechie_Labs](https://www.reddit.com/r/YesTechie_Labs/) to ask questions, suggest features and report bugs.

## License

Licensed under PolyForm Strict 1.0.0. Personal use costs nothing. The code is public to read, but you may not modify, share or resell it. For commercial use, contact [YesTechie](https://yestechie.com).

Not affiliated with Ubiquiti Inc.
