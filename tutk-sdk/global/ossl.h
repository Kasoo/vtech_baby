/* The host OpenSSL 3 functions Global calls itself. Declared here because the NDK has no OpenSSL
 * headers; every handle is opaque. tools/gen_stubs.py reads the OSSL_FN names to build the link-time
 * stand-in for libssl.so.3, so each function is declared on one OSSL_FN line. */
#ifndef TK_OSSL_H
#define TK_OSSL_H

#include <stddef.h>

typedef struct bio_st BIO;
typedef struct rsa_st RSA;
typedef struct ec_key_st EC_KEY;
typedef struct evp_md_st EVP_MD;

#define RSA_NO_PADDING            3
#define NID_X9_62_prime256v1      415

#define OSSL_FN(ret, name, args) ret name args;

OSSL_FN(int, RAND_bytes, (unsigned char *buf, int num))
OSSL_FN(unsigned char *, SHA256, (const unsigned char *d, size_t n, unsigned char *md))
OSSL_FN(const EVP_MD *, EVP_sha1, (void))
OSSL_FN(unsigned char *, HMAC, (const EVP_MD *md, const void *key, int key_len, const unsigned char *d, size_t n, unsigned char *out, unsigned int *out_len))
OSSL_FN(BIO *, BIO_new_mem_buf, (const void *buf, int len))
OSSL_FN(int, BIO_free, (BIO *b))
OSSL_FN(RSA *, PEM_read_bio_RSA_PUBKEY, (BIO *b, RSA **x, void *cb, void *u))
OSSL_FN(RSA *, PEM_read_bio_RSAPrivateKey, (BIO *b, RSA **x, void *cb, void *u))
OSSL_FN(int, RSA_size, (const RSA *r))
OSSL_FN(void, RSA_free, (RSA *r))
OSSL_FN(int, RSA_public_encrypt, (int n, const unsigned char *from, unsigned char *to, RSA *r, int padding))
OSSL_FN(int, RSA_public_decrypt, (int n, const unsigned char *from, unsigned char *to, RSA *r, int padding))
OSSL_FN(int, RSA_private_encrypt, (int n, const unsigned char *from, unsigned char *to, RSA *r, int padding))
OSSL_FN(int, RSA_private_decrypt, (int n, const unsigned char *from, unsigned char *to, RSA *r, int padding))
OSSL_FN(EC_KEY *, EC_KEY_new_by_curve_name, (int nid))
OSSL_FN(int, EC_KEY_generate_key, (EC_KEY *k))
OSSL_FN(void, EC_KEY_free, (EC_KEY *k))

#undef OSSL_FN

#endif
