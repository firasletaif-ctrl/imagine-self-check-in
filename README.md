# Imagine Self Check-in

Borne tactile d’inscription et de check-in, pensée pour iPad et écran de kiosque. L’interface est disponible en français, anglais et arabe (RTL). Les visiteurs peuvent retrouver une inscription existante avec leur e-mail ou leur code de badge, ou s’inscrire sur place. Les arrivées sont enregistrées dans **Imagine Desk**.

## Fonctionnement et sécurité

Le navigateur de la borne ne contacte jamais directement Imagine Desk et ne reçoit jamais ses identifiants. Le petit serveur Python de ce projet utilise un compte d’équipe dédié, conservé uniquement dans ses variables d’environnement, puis appelle l’API existante d’Imagine Desk. L’API du Desk permet déjà de lire l’événement et les inscrits, de créer un inscrit et de confirmer une arrivée : aucune modification du dépôt `imagine-desk` n’est nécessaire pour ce MVP.

**Créer dans Imagine Desk un compte agent dédié à la borne**, avec un mot de passe unique, pour les actions courantes. Ne mettez jamais ses identifiants dans un fichier versionné, dans le navigateur ou sur l’iPad. Servez les deux projets en HTTPS pour un vrai événement. L’inscription libre attribue la catégorie `Participant` et marque immédiatement l’arrivée; elle exige un nom, un e-mail et le consentement affiché. L’organisation est facultative. Un e-mail déjà présent est renvoyé vers le parcours des inscrits afin d’éviter les doublons.

Une **plateforme d’administration** est disponible à `/admin`. Connectez-vous avec un compte ayant le rôle administrateur dans Imagine Desk. Vous pouvez choisir l’événement actif présenté sur la borne, modifier son nom, sa date et son lieu (les changements sont aussi écrits dans Imagine Desk), et ouvrir ou fermer les inscriptions sur place. Les réglages de la borne (événement sélectionné et inscriptions ouvertes/fermées) sont conservés dans PostgreSQL lorsque `SELF_CHECKIN_DATABASE_URL` est défini; sinon, ils sont enregistrés dans `data/kiosk_settings.json`. Le mot de passe administrateur n’est pas enregistré par la borne; la session d’administration reste en mémoire et expire au redémarrage du serveur.

Le serveur n’expose pas la liste des participants au navigateur : il ne renvoie que le résultat de la recherche exacte par e-mail/code. Il impose aussi une limitation de tentatives et n’écrit pas les données personnelles dans ses journaux HTTP. Pour une exploitation professionnelle, placez-le derrière un reverse proxy HTTPS et ajoutez une politique de confidentialité adaptée à votre événement.

## Configuration

Python 3.10 ou plus récent, sans dépendance externe :

```bash
export IMAGINE_DESK_URL="https://votre-desk.example.tn"
export IMAGINE_DESK_USERNAME="kiosk.agent"
export IMAGINE_DESK_PASSWORD="mot-de-passe-unique-et-long"
# Facultatif au premier lancement : l’événement se choisit ensuite dans /admin
export IMAGINE_DESK_EVENT_ID="identifiant-de-l-evenement"
python server.py
```

`IMAGINE_DESK_EVENT_ID` est facultatif : s’il est défini, il sert d’événement initial. Sinon, après lancement, ouvrez `/admin` et choisissez un événement. Pour la première connexion à `/admin`, utilisez un **compte administrateur Imagine Desk**; le compte agent configuré pour la borne ne peut pas modifier les événements. Ne mettez pas de vrais identifiants dans un `.env` versionné.

La borne est alors accessible sur `http://adresse-du-serveur:8000` et l’administration sur `http://adresse-du-serveur:8000/admin`. L’application refuse une URL Desk non sécurisée, sauf `localhost` pour le développement. Les variables `HOST` (défaut `0.0.0.0`) et `PORT` (défaut `8000`) règlent l’écoute locale. Pour Internet, ajoutez un hébergement HTTPS; le serveur de développement intégré n’est pas un serveur de production.

### Déploiement en conteneur / plateforme

Définissez `IMAGINE_DESK_URL`, `IMAGINE_DESK_USERNAME` et `IMAGINE_DESK_PASSWORD` dans le panneau **Secrets / Environment** de l’hébergeur, et laissez `PORT` fourni par la plateforme. `IMAGINE_DESK_EVENT_ID` est facultatif. Commande de démarrage : `python server.py`. Ne copiez pas les identifiants dans la configuration du navigateur ou dans les paramètres du raccourci iPad. Le compte agent doit être actif et avoir accès à l’événement choisi. Le Blueprint `render.yaml` crée un service Web et une petite base PostgreSQL séparée, utilisée uniquement pour conserver les réglages de la borne. Les données des participants restent dans Imagine Desk et sont manipulées via son API; elles ne sont pas dupliquées dans cette base. Le service Web doit recevoir `IMAGINE_DESK_URL`, `IMAGINE_DESK_USERNAME` et `IMAGINE_DESK_PASSWORD` dans **Environment → Secrets**; le compte agent doit être actif et autorisé à ouvrir une session. L’identifiant de l’événement se choisit dans `/admin`. Le compte administrateur Imagine Desk est saisi au moment de se connecter à `/admin`, jamais dans les variables du service.

Pour Render : créez un **Blueprint** depuis ce dépôt et déployez les deux ressources déclarées (`imagine-self-check-in` et `imagine-self-check-in-db`). Ajoutez ensuite les trois secrets d’Imagine Desk au service Web. Vérifiez que l’état de santé `/api/health` est vert, puis connectez-vous à `/admin`, choisissez l’événement et enregistrez les réglages. La base déclarée ici est séparée de celle du Desk; elle ne stocke que l’identifiant choisi et l’ouverture des inscriptions. Vérifiez la durée de conservation et les limites du plan Render choisi avant un événement réel; utilisez un plan avec persistance adaptée et préparez des sauvegardes.

## Utilisation iPad / borne

1. Démarrer Imagine Desk et cette borne sur des adresses accessibles par le même réseau ou Internet.
2. Ouvrir l’adresse de la borne dans Safari ou Chromium. Il faut HTTPS si la borne est hébergée en ligne.
3. Sur iPad, Safari → **Partager → Sur l’écran d’accueil**, puis activer l’accès guidé pour verrouiller l’appareil sur la borne. Sur une borne Android/Windows, activer le mode kiosque du navigateur ou du système.
4. Tester avant l’événement : une inscription nouvelle, une recherche par e-mail, une recherche par code, un doublon, un retour arrière, et la synchronisation visible dans Imagine Desk.

## Périmètre du MVP / suites possibles

- Inscription express sur place : nom, e-mail, organisation; inscrit dans Imagine Desk et arrivée confirmée.
- Check-in des inscrits : recherche exacte e-mail ou code, confirmation explicite avant d’enregistrer l’arrivée.
- Interface tactile responsive et langues FR / EN / AR.
- Aucun scan caméra/QR pour l’instant (saisie du code possible); aucune impression de badge depuis la borne.
- Le tarif, les champs obligatoires, le texte de confidentialité, les règles de doublon et l’envoi de confirmation e-mail sont spécifiques à chaque événement et devront être ajustés avant la production.
- La limitation de tentatives est en mémoire par processus : plusieurs réplicas derrière un proxy nécessiteraient une limite partagée au niveau du proxy.

L’aperçu local peut afficher le formulaire sans configuration. Pour l’utiliser réellement, déployez les ressources Render, configurez les trois secrets du compte agent, choisissez un événement dans `/admin` avec un compte administrateur Imagine Desk, puis vérifiez la connexion avant le jour de l’événement. Localement, `SELF_CHECKIN_DATABASE_URL` est facultatif : sans elle, la borne garde ses réglages dans le fichier JSON ignoré par Git.
