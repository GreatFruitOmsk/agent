[![CircleCI](https://circleci.com/gh/WoTTsecurity/agent.svg?style=svg)](https://circleci.com/gh/WoTTsecurity/agent) [![Codacy Badge](https://api.codacy.com/project/badge/Grade/9e165c20e9b04d62a15d1ff7c4736878)](https://www.codacy.com/app/vpetersson/agent)

# WoTT IoT Agent

**WARNING:** WoTT is currently in beta. Use in production at your own risk.

## What is this?

**tl;dr** Simplified security for IoT developers

The goals for WoTT Agent is to:

 * Simplify encryption of device communication
 * Provide cryptographic identity of sender (such that the receiver can trust that the sender is who it claims to be)
 * Enable you to remove hard coded credentials from your application and firmware

The first build-block we need in order to facilitate encrypted communication between two peers is a cryptographic certificate [1]. This is provisioned automatically through the WoTT agent. At its core, this serves both as the means to enable encrypted communication, as well as each unique device’s identity.

With the certificate installed on the device, we’re able to establish connections to devices and services and cryptographically prove we are whom we claim to be [2]. It’s worth pointing out that this is different than how say your browser works. In such scenario, you as the client (i.e. the browser) verifies that the remote server (e.g. https://www.google.com) is indeed the being served from Google’s server and not an impersonator. There is however no way for Google to cryptographically know that you are who are (which is why you need to login in order to access your email). With WoTT however, we’re able to add this piece, which essentially means that there is no longer a need for username and passwords, since we can cryptographically prove that the client/user is indeed who he/she/it claims to be.

* [1] We do this by issuing an x509 certificate from our own Certificate Authority (CA).
* [2] This is done using something called Mutual TLS, or mTLS for short.


## Installation

Supported hardware:

* Raspberry Pi

### Installing on Raspbian

Recommended installation steps:

```
$ curl -s https://packagecloud.io/install/repositories/wott/agent/script.deb.sh | sudo bash
$ sudo apt install wott-agent
```

#### Alternative runtime environments

 * [Python library](https://github.com/WoTTsecurity/agent/blob/master/docs/alternative_installation_methods.md#installation--python-runtime-advance://github.com/WoTTsecurity/agent/blob/master/docs/alternative_installation_methods.md#installation--python-runtime-advanced)

## Use Cases

The certificates used for the WoTT agent can be use for a number of use cases. Here are some ideas to help you get started:

### Credential Management

 * To be written

### TLS/mTLS Access Control

* To be written

### Outdated

 * [Simple WebApp](https://github.com/WoTTsecurity/agent/tree/master/docs/examples/simple-webapp)
 * [Web of Things](https://github.com/WoTTsecurity/agent/tree/master/docs/examples/webofthings)
 * [Google Core IoT](https://github.com/WoTTsecurity/agent/tree/master/docs/examples/google-core-iot)
 * [Nginx (mTLS)](https://github.com/WoTTsecurity/agent/tree/master/docs/examples/nginx)
