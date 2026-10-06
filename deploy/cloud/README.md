# GHOST en la nube (sin depender de la Mac)

Los bots (GHOST y ORBIT), la página web, la app y las notificaciones corren en
un servidor en internet, 24/7. La Mac ya no tiene que estar prendida.

Todo es en papel: ninguna orden es real.

## Qué necesitas

**Un servidor Linux (Ubuntu 24.04) con 2 GB de RAM**, cerca de Nueva York.
Opciones probadas para este tipo de bot:

| Proveedor | Plan | Ubicación | Precio aprox. |
|---|---|---|---|
| Hetzner Cloud | CPX11 (2 vCPU, 2 GB) | Ashburn, VA | ~US$5–6 al mes |
| DigitalOcean | Basic 2 GB | New York | ~US$12 al mes |

Los precios cambian: revisa el sitio del proveedor.

## Mover los bots (una sola vez)

1. En la Mac, saca la llave SSH:

   ```
   bash deploy/macos/ctl.sh nube llave
   ```

2. Crea el servidor en el proveedor:
   - Ubuntu 24.04.
   - Pega esa llave en "SSH keys".
   - Anota la IP que te dan.

3. En la Mac:

   ```
   bash deploy/macos/ctl.sh nube IP-DEL-SERVIDOR
   ```

   Ese comando hace todo:
   - copia el código, `data_store/` (trades, usuarios, set ups, práctica de cada usuario), `market_data/` y el `.env`;
   - apaga los bots de la Mac, para que nada avise dos veces;
   - instala Docker en el servidor;
   - enciende GHOST, ORBIT y HTTPS.

   La primera vez tarda unos minutos.

4. Durante la instalación te pide un **token de GitHub de solo lectura**. Es lo
   que permite que "Buscar actualización e instalar" funcione en el servidor,
   porque el repositorio es privado. Se escribe ahí y se guarda solo en el
   servidor. Si lo saltas, actualizas corriendo el paso 3 otra vez.

   Para crearlo: github.com → Settings → Developer settings → Fine-grained
   tokens → elige este repositorio → Contents: **Read-only**.

5. Al final te da la dirección nueva, por ejemplo
   `https://5-6-7-8.sslip.io`. HTTPS tarda hasta un minuto la primera vez.

## Después de mover

- **Entrar:** con tu mismo usuario (Rickybobby). Los usuarios se copiaron, pero
  como la dirección es otra, hay que entrar una vez.
- **App del iPhone:**
  1. Ábrela en Safari con la dirección nueva.
  2. Compartir → **Agregar a inicio**.
  3. Activa otra vez las notificaciones en Sistema.
  4. Borra la app vieja.
- **Link para practicar:** `https://TU-DIRECCION/practica`. Quien lo abre se
  crea su usuario.
- **Discord:** sigue igual, con los mismos webhooks del `.env`.
- **Tailscale Funnel de la Mac:** ya no hace falta:

  ```
  bash deploy/macos/ctl.sh web off
  ```

## Actualizar

- **Desde el teléfono:** Sistema → **Buscar actualización e instalar**. GHOST y
  ORBIT se reinician solos.
- **O desde la Mac:** `bash deploy/macos/ctl.sh nube IP-DEL-SERVIDOR`. Cuando los
  bots ya viven allá, solo manda el código: los datos del servidor no se tocan.

## Tu propio dominio (opcional)

En el servidor, cambia `DOMAIN=` en `/opt/ghost/.env` por tu dominio (con su
registro A apuntando a la IP del servidor) y corre:

```
cd /opt/ghost && docker compose up -d
```

## Si algo falla

- **Ver qué pasa (en el servidor):**

  ```
  cd /opt/ghost && docker compose logs -f ghost
  ```

- **"Sin datos" o "DISCONNECTED":** Yahoo a veces bloquea servidores en la
  nube. La página sigue funcionando y el bot reintenta solo cada ciclo. Si no
  vuelve en un rato, prueba otra ubicación del servidor (otra IP) o mueve los
  bots de regreso a la Mac.
- **Volver a la Mac:**
  1. En el servidor:

     ```
     cd /opt/ghost && docker compose down
     ```

  2. En la Mac, trae los datos (trades, usuarios, práctica):

     ```
     bash deploy/macos/ctl.sh nube traer IP-DEL-SERVIDOR
     ```

  3. En la Mac:

     ```
     bash deploy/macos/ctl.sh nube volver
     ```
